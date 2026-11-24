"""小助 —— 基于 Chatbot Starter 改造的个人助理。

核心能力（也是模板的六个部件）：
01 接入大模型     FastAPI /api/chat 转发 OpenAI 兼容接口，key、地址、模型名从 .env 读
02 System Prompt  prompts/system.md 写人设与规则，prompts/profile.md 放「关于我」的资料
03 上下文         完整历史随请求送入模型；超过阈值自动压缩成备忘（memory）
04 流式输出       /api/chat 是 SSE，模型每生成一段就推给前端
05 停止/重新生成   前端中断请求，后端停止生成并保存半截回复
06 会话存储       SQLite 存会话、消息、备忘，前端只传 session_id + 这一句
07 长期记忆       facts 表存「关于我」的事实，每次对话注入 system prompt，抽屉里可增删

想改成你的助手：改 prompts/ 和 .env 就够了，代码不用动。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chatbot")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# 配置：模型与存储
# ---------------------------------------------------------------------------

# 兼容旧名字：LLM_* 优先，其次 DEEPSEEK_*
LLM_API_KEY = (os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")).strip()

# 本地模型（Ollama / LM Studio / vLLM / 局域网网关）通常不需要 key。
# 只有官方云服务才强制要求填 key，其余情况没填也能直接跑。
OFFICIAL_HOSTS = ("api.deepseek.com", "api.openai.com")


def needs_api_key() -> bool:
    if LLM_API_KEY:
        return False
    host = (urlparse(LLM_BASE_URL).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LLM_API_KEY or 'local'}", "Content-Type": "application/json"}

LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
LLM_MODEL = (os.getenv("LLM_MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip() or "deepseek-chat"
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "chatbot.db")))
CONTEXT_TOKEN_LIMIT = int(os.getenv("CONTEXT_TOKEN_LIMIT", "2000"))
KEEP_RECENT_MESSAGES = max(2, int(os.getenv("KEEP_RECENT_MESSAGES", "4")))
MAX_FACTS = max(10, int(os.getenv("MAX_FACTS", "60")))

# ---------------------------------------------------------------------------
# 配置：品牌与文案（改 .env 即可换成你的机器人，不用碰 HTML）
# ---------------------------------------------------------------------------

BOT_NAME = os.getenv("BOT_NAME", "小助")
BOT_EMOJI = os.getenv("BOT_EMOJI", "🧭")
BOT_TAGLINE = os.getenv("BOT_TAGLINE", "个人助理")
PAGE_TITLE = os.getenv("PAGE_TITLE", f"{BOT_NAME} · 我的助理")
WELCOME_TITLE = os.getenv("WELCOME_TITLE", "今天想先处理哪件事？")
WELCOME_SUB = os.getenv(
    "WELCOME_SUB",
    "日程、待办、写东西、做决定，都可以丢给我；关于你的事我会记在「关于我」里。",
)
INPUT_HINT = os.getenv("INPUT_HINT", "说件事，或者贴一段要改的文字…")
ACCENT = os.getenv("BOT_ACCENT", "#2f6f6b")
ACCENT_SOFT = os.getenv("BOT_ACCENT_SOFT", "#e3efed")
STORAGE_KEY = os.getenv("STORAGE_KEY", "assistant.session")
EXAMPLE_QUESTIONS = [
    q.strip()
    for q in os.getenv(
        "EXAMPLE_QUESTIONS",
        "帮我排一下今天的待办|写封邮件请假一天|这个周末怎么安排比较好|帮我把这段话改得更客气",
    ).split("|")
    if q.strip()
][:4]

# ---------------------------------------------------------------------------
# 02 System Prompt：prompts/system.md + prompts/handbook.md
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_PATH = BASE_DIR / os.getenv("SYSTEM_PROMPT_FILE", "prompts/system.md")
HANDBOOK_PATH = BASE_DIR / os.getenv("HANDBOOK_FILE", "prompts/profile.md")

DEFAULT_SYSTEM_PROMPT = """你是一个乐于助人的对话助手。

## 怎么回答
1. 先给一句结论，再给可执行的步骤。
2. 语气自然、口语化，不堆客套话。
3. 用户信息不够时先问清楚再答。

## 不知道时怎么办
- 不编造事实、数字和规则。
- 直接说「这条我拿不准」，说明建议去问谁，并把需要确认的点列出来。
"""


def load_system_prompt() -> str:
    """人设规则 + 业务手册拼成完整 system prompt。"""
    parts: list[str] = []
    if SYSTEM_PROMPT_PATH.exists():
        parts.append(SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip())
    else:
        logger.warning("没找到 %s，使用内置默认 prompt", SYSTEM_PROMPT_PATH.relative_to(BASE_DIR))
        parts.append(DEFAULT_SYSTEM_PROMPT)
    if HANDBOOK_PATH.exists():
        handbook = HANDBOOK_PATH.read_text(encoding="utf-8").strip()
        if handbook:
            parts.append("## 关于我（用户资料，回答时的背景；和用户当轮说法冲突时以当轮为准）\n\n" + handbook)
    return "\n\n".join(p for p in parts if p)


SYSTEM_PROMPT = load_system_prompt()

SUMMARY_PROMPT = """你在为一段对话做上下文压缩。请把下面的旧对话整理成一份备忘，只保留后续回答还需要的事实：用户是谁（姓名、身份）、涉及的对象、金额、日期、编号、已给出的结论和未解决的问题。用短条目列出，不要寒暄，不要编造。直接输出备忘正文。"""

# ---------------------------------------------------------------------------
# 06 SQLite 会话存储
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '新对话',
    memo       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    stopped    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


def create_session() -> dict[str, Any]:
    now = time.time()
    sid = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions(id, title, memo, created_at, updated_at) VALUES(?,?,?,?,?)",
            (sid, "新对话", "", now, now),
        )
    return get_session(sid)


def get_session(session_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, title, memo, created_at, updated_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, memo, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def rename_session(session_id: str, title: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (title[:40], time.time(), session_id),
        )


def set_memo(session_id: str, memo: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET memo=?, updated_at=? WHERE id=?",
            (memo, time.time(), session_id),
        )


def load_messages(session_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, stopped, created_at FROM messages "
            "WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_message(session_id: str, role: str, content: str, stopped: bool = False) -> int:
    now = time.time()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO messages(session_id, role, content, stopped, created_at) VALUES(?,?,?,?,?)",
            (session_id, role, content, 1 if stopped else 0, now),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        return int(cur.lastrowid)


def delete_messages(session_id: str, ids: list[int]) -> None:
    if not ids:
        return
    with db() as conn:
        conn.executemany("DELETE FROM messages WHERE id=? AND session_id=?", [(i, session_id) for i in ids])
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))


def delete_last_assistant(session_id: str) -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))


def last_user_message(session_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 07 长期记忆：关于用户的事实，跨会话有效
# ---------------------------------------------------------------------------


def list_facts() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT id, content, created_at FROM facts ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def add_fact(content: str) -> dict[str, Any]:
    content = content.strip()[:500]
    if not content:
        raise ValueError("记忆内容不能为空")
    with db() as conn:
        exists = conn.execute("SELECT id FROM facts WHERE content=?", (content,)).fetchone()
        if exists:
            row = conn.execute(
                "SELECT id, content, created_at FROM facts WHERE id=?", (exists["id"],)
            ).fetchone()
            return dict(row)
        cur = conn.execute("INSERT INTO facts(content, created_at) VALUES(?,?)", (content, time.time()))
        conn.execute(
            "DELETE FROM facts WHERE id NOT IN (SELECT id FROM facts ORDER BY id DESC LIMIT ?)",
            (MAX_FACTS,),
        )
        row = conn.execute(
            "SELECT id, content, created_at FROM facts WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def delete_fact(fact_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))


# ---------------------------------------------------------------------------
# 03 上下文组装与压缩
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：一个汉字约 1 token，其余按 4 字符 1 token。"""
    cjk = len(_CJK.findall(text))
    return cjk + max(1, (len(text) - cjk) // 4)


def build_chat_messages(session: dict[str, Any], history: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = SYSTEM_PROMPT
    facts = list_facts()
    if facts:
        system += (
            "\n\n## 关于用户的长期记忆（跨会话保存，可信；和用户本轮说法冲突时以本轮为准）\n"
            + "\n".join(f"- {f['content']}" for f in facts)
        )
    memo = (session.get("memo") or "").strip()
    if memo:
        system += "\n\n## 更早对话的备忘（由系统压缩，内容可信）\n" + memo
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] in ("user", "assistant") and m["content"].strip():
            messages.append({"role": m["role"], "content": m["content"]})
    return messages


def history_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(m["content"]) for m in messages)


async def call_model_once(messages: list[dict[str, str]], max_tokens: int = 800) -> str:
    if needs_api_key():
        raise RuntimeError("服务端没有配置 LLM_API_KEY")
    payload = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens, "stream": False}
    headers = auth_headers()
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15)) as client:
        resp = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"上游返回 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()


async def compress_history(
    session_id: str, session: dict[str, Any], history: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把最近 KEEP_RECENT_MESSAGES 条之外的旧对话压缩成备忘，写进 sessions.memo，并删除旧原文。"""
    cut = max(0, len(history) - KEEP_RECENT_MESSAGES)
    older = history[:cut]
    recent = history[cut:]
    # 保证保留的几条从「用户」开始，避免上下文出现没有提问的助手回复
    if recent and recent[0]["role"] == "assistant" and len(recent) > 1:
        older.append(recent.pop(0))
    if not older:
        return session, history

    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}" for m in older
    )
    old_memo = (session.get("memo") or "").strip()
    prompt = (
        f"{SUMMARY_PROMPT}\n\n## 已有备忘\n{old_memo or '（暂无）'}\n\n"
        f"## 需要压缩的旧对话\n{transcript}\n\n"
        "## 输出\n请输出合并后的完整备忘（旧备忘 + 新信息，去掉重复），不要丢关键事实。"
    )
    summary = await call_model_once(
        [
            {"role": "system", "content": "你是严谨的对话压缩器，只输出备忘正文。"},
            {"role": "user", "content": prompt},
        ],
        max_tokens=700,
    )
    memo = summary or old_memo
    if len(memo) > 6000:
        memo = memo[-6000:]
    set_memo(session_id, memo)
    delete_messages(session_id, [m["id"] for m in older])
    logger.info("压缩会话 %s：%d 条旧消息 → 备忘 %d 字", session_id[:8], len(older), len(memo))
    return get_session(session_id) or session, load_messages(session_id)


# ---------------------------------------------------------------------------
# 01/04/05 模型流式调用 + SSE
# ---------------------------------------------------------------------------


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def stream_model(messages: list[dict[str, str]]) -> AsyncIterator[str]:
    if needs_api_key():
        raise RuntimeError("服务端没有配置 LLM_API_KEY，请先在 .env 里填好再试。")
    payload = {"model": LLM_MODEL, "messages": messages, "temperature": 0.5, "max_tokens": LLM_MAX_TOKENS, "stream": True}
    headers = auth_headers()
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
        async with client.stream("POST", f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"上游返回 {resp.status_code}: {body[:300]}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta


async def reply_stream(request: Request, session_id: str) -> AsyncIterator[str]:
    session = get_session(session_id)
    if session is None:
        yield sse({"type": "error", "message": "会话不存在，请新建会话"})
        return

    history = load_messages(session_id)
    if not history or history[-1]["role"] != "user":
        yield sse({"type": "error", "message": "没有找到要回复的用户消息"})
        return

    messages = build_chat_messages(session, history)
    if history_tokens(messages) > CONTEXT_TOKEN_LIMIT and len(history) > KEEP_RECENT_MESSAGES + 1:
        try:
            session, history = await compress_history(session_id, session, history)
            messages = build_chat_messages(session, history)
            yield sse({"type": "memo", "memo": session.get("memo", "")})
        except Exception as exc:  # 压缩失败不应该中断对话
            logger.warning("压缩失败，继续用原文: %s", exc)

    yield sse({"type": "start", "session_id": session_id, "model": LLM_MODEL})

    parts: list[str] = []
    stopped = False
    try:
        async for delta in stream_model(messages):
            if await request.is_disconnected():
                stopped = True
                break
            parts.append(delta)
            yield sse({"type": "delta", "text": delta})
    except asyncio.CancelledError:
        if parts:
            add_message(session_id, "assistant", "".join(parts), stopped=True)
        raise
    except Exception as exc:
        logger.warning("模型调用失败: %s", exc)
        if parts:
            add_message(session_id, "assistant", "".join(parts), stopped=True)
        yield sse({"type": "error", "message": str(exc)})
        return

    text = "".join(parts)
    if text.strip():
        add_message(session_id, "assistant", text, stopped=stopped)
    yield sse({"type": "done", "stopped": stopped, "session_id": session_id})


# ---------------------------------------------------------------------------
# 页面渲染：把 .env 里的品牌文案注入 static/index.html
# ---------------------------------------------------------------------------

PAGE_PATH = BASE_DIR / "static" / "index.html"
_page_cache: str | None = None


def render_page() -> str:
    global _page_cache
    if _page_cache is not None:
        return _page_cache
    html = PAGE_PATH.read_text(encoding="utf-8")
    config = {
        "name": BOT_NAME,
        "welcome_title": WELCOME_TITLE,
        "welcome_sub": WELCOME_SUB,
        "examples": EXAMPLE_QUESTIONS,
        "storage_key": STORAGE_KEY,
        "input_hint": INPUT_HINT,
    }
    # 放在 <script type="application/json"> 里，转义 </ 防止提前闭合
    config_json = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    tokens = {
        "{{PAGE_TITLE}}": PAGE_TITLE,
        "{{BOT_NAME}}": BOT_NAME,
        "{{BOT_EMOJI}}": BOT_EMOJI,
        "{{BOT_TAGLINE}}": BOT_TAGLINE,
        "{{INPUT_HINT}}": INPUT_HINT,
        "{{ACCENT}}": ACCENT,
        "{{ACCENT_SOFT}}": ACCENT_SOFT,
        "{{CONFIG_JSON}}": config_json,
    }
    for key, value in tokens.items():
        html = html.replace(key, value)
    _page_cache = html
    return html


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str


app = FastAPI(title=PAGE_TITLE)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    logger.info(
        "bot=%s db=%s model=%s key=%s prompt=%s",
        BOT_NAME,
        DB_PATH,
        LLM_MODEL,
        "已配置" if not needs_api_key() else "未配置",
        str(SYSTEM_PROMPT_PATH.relative_to(BASE_DIR)),
    )


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse(render_page())


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "bot": BOT_NAME,
        "model": LLM_MODEL,
        "key_configured": not needs_api_key(),
        "local_model": not LLM_API_KEY,
        "needs_api_key": needs_api_key(),
        "system_prompt": str(SYSTEM_PROMPT_PATH.relative_to(BASE_DIR)),
        "handbook": HANDBOOK_PATH.exists(),
        "facts": len(list_facts()),
    }


class FactIn(BaseModel):
    content: str


@app.get("/api/facts")
async def facts() -> list[dict[str, Any]]:
    return list_facts()


@app.post("/api/facts")
async def new_fact(body: FactIn) -> Any:
    try:
        return add_fact(body.content)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@app.delete("/api/facts/{fact_id}")
async def remove_fact(fact_id: int) -> dict[str, Any]:
    delete_fact(fact_id)
    return {"ok": True}


@app.get("/api/sessions")
async def sessions() -> list[dict[str, Any]]:
    return list_sessions()


@app.post("/api/sessions")
async def new_session() -> dict[str, Any]:
    return create_session()


@app.delete("/api/sessions/{session_id}")
async def remove_session(session_id: str) -> dict[str, Any]:
    delete_session(session_id)
    return {"ok": True}


@app.get("/api/sessions/{session_id}/messages")
async def messages(session_id: str) -> JSONResponse:
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "会话不存在"}, status_code=404)
    return JSONResponse({"session": session, "messages": load_messages(session_id)})


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request) -> StreamingResponse:
    text = body.message.strip()
    if not text:
        return JSONResponse({"detail": "消息不能为空"}, status_code=400)
    session_id = body.session_id
    if not session_id or get_session(session_id) is None:
        session_id = create_session()["id"]
    add_message(session_id, "user", text)
    session = get_session(session_id)
    if session and session["title"] == "新对话":
        rename_session(session_id, text[:24])
    return StreamingResponse(
        reply_stream(request, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/regenerate")
async def regenerate(session_id: str, request: Request) -> StreamingResponse:
    """05 重新生成：删掉最后一条回复，用最后一条用户消息重问。"""
    if get_session(session_id) is None:
        return JSONResponse({"detail": "会话不存在"}, status_code=404)
    if last_user_message(session_id) is None:
        return JSONResponse({"detail": "还没有可重答的问题"}, status_code=400)
    delete_last_assistant(session_id)
    return StreamingResponse(
        reply_stream(request, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
