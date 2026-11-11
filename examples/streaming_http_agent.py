"""Test adapter: HTTP Agent that yields SSE token fragments with deliberate latency.

Start with: uvicorn examples.streaming_http_agent:app --host 127.0.0.1 --port 19913
The bearer token is read from MESH_STREAM_AGENT_TOKEN.
"""
import asyncio
import json
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Demo streaming agent")

class Invoke(BaseModel):
    task_id: str
    input: str
    context: dict = {}

@app.post("/invoke")
async def invoke(body: Invoke, authorization: str | None = Header(default=None)):
    token = os.environ.get("MESH_STREAM_AGENT_TOKEN")
    if not token or authorization != "Bearer " + token:
        raise HTTPException(401, "Unauthorized")
    async def stream():
        text = "Streamed: " + body.input
        for part in (text[:5],text[5:11],text[11:]):
            await asyncio.sleep(.20)
            yield "event: delta\ndata: " + json.dumps({"text":part}) + "\n\n"
        await asyncio.sleep(.20)
        yield "event: done\ndata: " + json.dumps({"output":text}) + "\n\n"
    return StreamingResponse(stream(),media_type="text/event-stream")
