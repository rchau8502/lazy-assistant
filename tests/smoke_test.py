"""端到端自测：流式、停止、压缩、重新生成、长期记忆、提醒、搜索、会话删除。

默认打 mock 上游所在的端口，可用 BASE_URL 覆盖：
    BASE_URL=http://127.0.0.1:8000 python tests/smoke_test.py
"""
import json
import os
import time
from datetime import datetime, timedelta

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8012")


def read_sse(resp):
    events = []
    for line in resp.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def post_chat(cid, text, stream=True):
    with httpx.Client(timeout=60) as c:
        with c.stream("POST", f"{BASE}/api/chat", json={"session_id": cid, "message": text}) as r:
            assert r.status_code == 200, r.status_code
            return read_sse(r) if stream else None


def main():
    with httpx.Client(timeout=30) as c:
        sid = c.post(f"{BASE}/api/sessions").json()["id"]
        print("session", sid[:8])

        # 1) 流式
        events = post_chat(sid, "帮我排一下今天的待办")
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        print("stream events:", len(events), "| text chars:", len(text))
        print("first 40:", text[:40].replace("\n", " / "))
        assert events[0]["type"] == "start" and events[-1]["type"] == "done"
        data = c.get(f"{BASE}/api/sessions/{sid}/messages").json()
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"], data
        assert data["session"]["title"] == "帮我排一下今天的待办", data["session"]["title"]
        print("1) 流式 + 入库 OK")

        # 2) 停止：读几段就断开
        with httpx.Client(timeout=60) as c2:
            with c2.stream("POST", f"{BASE}/api/chat", json={"session_id": sid, "message": "那明天呢？"}) as r:
                got = 0
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        got += 1
                        if got >= 3:
                            break
            time.sleep(1.5)
        data = c.get(f"{BASE}/api/sessions/{sid}/messages").json()
        last = data["messages"][-1]
        print("2) 断开后最后一条:", last["role"], "stopped=", last["stopped"], "chars=", len(last["content"]))
        assert last["role"] == "assistant" and last["stopped"] == 1, last

        # 3) 压缩：连续对话，超过 200 token 后应产生 memo 且旧消息被删
        for i in range(7):
            events = post_chat(sid, f"第 {i + 1} 轮追问：下午还有空吗？")
            assert events[-1]["type"] == "done", events[-1]
        data = c.get(f"{BASE}/api/sessions/{sid}/messages").json()
        print("3) memo:", (data["session"]["memo"] or "")[:50])
        print("   messages left:", len(data["messages"]), [m["role"] for m in data["messages"]])
        assert data["session"]["memo"].strip(), "压缩没有生成备忘"
        assert len(data["messages"]) <= 6, "旧消息没有被删"

        # 4) 重新生成
        before = c.get(f"{BASE}/api/sessions/{sid}/messages").json()["messages"]
        with httpx.Client(timeout=60) as c3:
            with c3.stream("POST", f"{BASE}/api/sessions/{sid}/regenerate") as r:
                events = read_sse(r)
        after = c.get(f"{BASE}/api/sessions/{sid}/messages").json()["messages"]
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        print("4) 重答 chars:", len(text), "| assistant 条数:", sum(m["role"] == "assistant" for m in before), "->", sum(m["role"] == "assistant" for m in after))
        assert sum(m["role"] == "assistant" for m in after) == sum(m["role"] == "assistant" for m in before), "重答应替换而不是追加"

        # 5) 长期记忆：新增、去重、列表、删除
        f = c.post(f"{BASE}/api/facts", json={"content": "称呼：阿明"}).json()
        assert f["content"] == "称呼：阿明", f
        dup = c.post(f"{BASE}/api/facts", json={"content": "称呼：阿明"}).json()
        assert dup["id"] == f["id"], "重复内容应该去重"
        facts = c.get(f"{BASE}/api/facts").json()
        assert any(x["id"] == f["id"] for x in facts), facts
        assert c.get(f"{BASE}/api/health").json()["facts"] >= 1
        assert c.delete(f"{BASE}/api/facts/{f['id']}").status_code == 200
        assert not any(x["id"] == f["id"] for x in c.get(f"{BASE}/api/facts").json())
        print("5) 长期记忆 OK")

        # 6) 工具：对话里设提醒（mock 上游会发 create_reminder 工具调用）
        sid2 = c.post(f"{BASE}/api/sessions").json()["id"]
        events = post_chat(sid2, "明天 9 点提醒我交周报")
        names = [(e.get("name"), e.get("status")) for e in events if e["type"] == "tool"]
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        print("6) 工具事件:", names, "| 回复:", text[:30])
        assert ("create_reminder", "done") in names, names
        rems = c.get(f"{BASE}/api/reminders").json()
        assert any("周报" in r["content"] for r in rems), rems
        rid = next(r["id"] for r in rems if "周报" in r["content"])

        # 7) 提醒接口：过去太久的拒绝、到点能查、fired 后不再出现
        bad = c.post(f"{BASE}/api/reminders", json={"content": "过期", "due_at": "2020-01-01T09:00:00+08:00"})
        assert bad.status_code == 400, bad.text
        soon = (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(timespec="seconds")
        r3 = c.post(f"{BASE}/api/reminders", json={"content": "到点测试", "due_at": soon}).json()
        due = c.get(f"{BASE}/api/reminders/due").json()
        assert any(x["id"] == r3["id"] for x in due), due
        assert c.post(f"{BASE}/api/reminders/{r3['id']}/fired").status_code == 200
        assert not any(x["id"] == r3["id"] for x in c.get(f"{BASE}/api/reminders/due").json())
        print("7) 提醒接口 OK（新增、到期查询、fired、过去时间 400）")

        # 8) 联网搜索：web_search 工具调用 + mock /search
        events = post_chat(sid2, "搜一下今天的 AI 新闻")
        names = [e.get("name") for e in events if e["type"] == "tool"]
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        print("8) 搜索工具:", names, "| 回复:", text[:40])
        assert "web_search" in names and "查到" in text, (names, text)

        # 清理提醒与测试会话
        for rid in (rid, r3["id"]):
            assert c.delete(f"{BASE}/api/reminders/{rid}").status_code == 200
        assert not c.get(f"{BASE}/api/reminders").json()

        # 9) 删除会话
        assert c.delete(f"{BASE}/api/sessions/{sid}").status_code == 200
        assert c.delete(f"{BASE}/api/sessions/{sid2}").status_code == 200
        sessions = c.get(f"{BASE}/api/sessions").json()
        assert not any(s["id"] in (sid, sid2) for s in sessions)
        print("9) 删除会话 OK")
        print("\nALL PASS")


if __name__ == "__main__":
    main()
