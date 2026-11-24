"""本地假的模型上游：没有真 key 时验证 SSE 流式、压缩、停止、工具调用。

启动：
    .venv/bin/uvicorn --app-dir tests mock_upstream:app --port 8011
"""
import asyncio
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

REPLY = (
    "先说结论：今天先保两件必须交的，其余往后排。\n"
    "1. 上午 9:30-11:00 写方案初稿，这段时间别开会。\n"
    "2. 中午把评审意见收齐，下午 2 点前回复。\n"
    "3. 剩下的事挪到明天，今天只留一个机动小时。\n\n"
    "要不要我按这个顺序帮你写成清单？"
)

SUMMARY = "备忘：用户让排今天的待办；已建议上午写初稿、中午收意见、其余顺延。"


def sse_delta(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def stream_text(text: str):
    for i in range(0, len(text), 6):
        yield sse_delta({"choices": [{"delta": {"content": text[i : i + 6]}}]})
        await asyncio.sleep(0.1)
    yield "data: [DONE]\n\n"


async def stream_tool_call(name: str, arguments: str):
    first = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_mock_1",
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
            }
        ]
    }
    yield sse_delta(first)
    for i in range(0, len(arguments), 16):
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": arguments[i : i + 16]}}
                        ]
                    }
                }
            ]
        }
        yield sse_delta(chunk)
        await asyncio.sleep(0.02)
    yield "data: [DONE]\n\n"


@app.post("/chat/completions")
async def completions(req: Request):
    body = await req.json()
    if "messages" not in body:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not body.get("stream"):
        return {"choices": [{"message": {"role": "assistant", "content": SUMMARY}}]}

    messages = body.get("messages") or []
    tools = body.get("tools") or []
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    user_text = (last_user or {}).get("content") or ""
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    # 已经拿到工具结果：给一句收尾回复
    if tools and tool_msgs:
        names = [
            call.get("function", {}).get("name", "")
            for m in messages
            if m.get("role") == "assistant"
            for call in (m.get("tool_calls") or [])
        ]
        name = names[-1] if names else ""
        result = tool_msgs[-1].get("content") or ""
        if name == "create_reminder":
            text = "好，提醒已经设好了。到点后打开小助才会响，别把页面全关了。"
        elif name == "web_search":
            try:
                answer = json.loads(result).get("answer") or ""
            except Exception:
                answer = ""
            text = "查到一条：" + (answer or "没搜到能用的结果。") if answer else "搜索没成功，先不编。"
        else:
            text = "好了。"
        return StreamingResponse(stream_text(text), media_type="text/event-stream")

    if tools and "提醒" in user_text:
        due = (datetime.now().astimezone() + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        args = json.dumps({"content": "交周报", "due_at": due.isoformat(timespec="seconds")}, ensure_ascii=False)
        return StreamingResponse(stream_tool_call("create_reminder", args), media_type="text/event-stream")

    if tools and ("搜" in user_text or "新闻" in user_text):
        args = json.dumps({"query": "今天的 AI 新闻"}, ensure_ascii=False)
        return StreamingResponse(stream_tool_call("web_search", args), media_type="text/event-stream")

    return StreamingResponse(stream_text(REPLY), media_type="text/event-stream")


@app.post("/search")
async def search(req: Request):
    body = await req.json()
    if not body.get("api_key"):
        return JSONResponse({"error": "missing api key"}, status_code=401)
    return {
        "answer": "模拟搜索：今天 AI 圈没有大事，都在等新模型。",
        "results": [
            {"title": "AI 日报", "url": "https://example.com/ai-daily", "content": "示例摘要。"}
        ],
    }
