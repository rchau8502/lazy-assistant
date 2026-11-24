"""本地假的模型上游：没有真 key 时验证 SSE 流式、压缩、停止。

启动：
    .venv/bin/uvicorn --app-dir tests mock_upstream:app --port 8011
"""
import asyncio
import json

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


@app.post("/chat/completions")
async def completions(req: Request):
    body = await req.json()
    if "messages" not in body:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not body.get("stream"):
        return {"choices": [{"message": {"role": "assistant", "content": SUMMARY}}]}

    async def gen():
        for i in range(0, len(REPLY), 6):
            chunk = REPLY[i : i + 6]
            data = {"choices": [{"delta": {"content": chunk}}]}
            yield "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0.1)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
