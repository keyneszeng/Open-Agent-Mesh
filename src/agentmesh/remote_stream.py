"""A2A 1.0 JSON-RPC streaming client subset: SendStreamingMessage over SSE.

Supports streamed task status/artifact updates and canonical final output.
It is not a complete A2A protocol client (no push subscriptions or file parts).
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, AsyncIterator

import httpx


class RemoteStreamError(Exception):
    pass


async def sse_frames(response: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
    """Incremental SSE JSON frames. Reject unbounded frames, malformed JSON and input."""
    name = "message"
    lines: list[str] = []
    async for line in response.aiter_lines():
        if len(line.encode("utf-8")) > 16384:
            raise RemoteStreamError("SSE line exceeds 16 KB")
        if line == "":
            if lines:
                try:
                    data = json.loads("\n".join(lines))
                except (json.JSONDecodeError, UnicodeError):
                    raise RemoteStreamError("Invalid SSE JSON") from None
                if not isinstance(data, dict):
                    raise RemoteStreamError("SSE data must be an object")
                yield name, data
            name, lines = "message", []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
            if sum(len(part.encode("utf-8")) for part in lines) > 16384:
                raise RemoteStreamError("SSE frame exceeds 16 KB")
        # SSE comments, ids and keepalives are ignored for this request stream.
    if lines:
        try:
            data = json.loads("\n".join(lines))
        except (json.JSONDecodeError, UnicodeError):
            raise RemoteStreamError("Invalid trailing SSE JSON") from None
        if not isinstance(data, dict):
            raise RemoteStreamError("Invalid trailing SSE data")
        yield name, data


def parts_value(parts: Any) -> Any:
    if not isinstance(parts, list):
        raise RemoteStreamError("Invalid A2A artifact parts")
    values = []
    for p in parts:
        if not isinstance(p, dict):
            raise RemoteStreamError("Invalid A2A artifact part")
        if "text" in p:
            if not isinstance(p["text"], str):
                raise RemoteStreamError("Invalid text part")
            values.append(p["text"])
        elif "data" in p:
            values.append(p["data"])
    return values[0] if len(values) == 1 else values


async def run_a2a_stream(client: httpx.AsyncClient, url: str, headers: dict,
                         task_id: str, value: Any, context: dict,
                         on_delta: Callable[[str], Awaitable[None]] | None) -> Any:
    request = {
        "jsonrpc": "2.0", "id": task_id, "method": "SendStreamingMessage",
        "params": {
            "message": {"messageId": task_id, "role": "ROLE_USER",
                        "parts": [{"text": value} if isinstance(value, str) else {"data": value}],
                        **({"contextId": context["_a2a_context_id"]} if context.get("_a2a_context_id") else {})},
            "configuration": {"returnImmediately": True},
        },
    }
    headers = dict(headers)
    headers["A2A-Version"] = "1.0"
    headers["Accept"] = "text/event-stream"
    chunks: list[str] = []
    cumulative = 0
    canonical_output = None
    complete = False
    async with client.stream("POST", url, headers=headers, json=request) as response:
        response.raise_for_status()
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
            raise RemoteStreamError("A2A streaming endpoint must return SSE")
        async for _, frame in sse_frames(response):
            if frame.get("jsonrpc") != "2.0" or frame.get("id") != task_id:
                raise RemoteStreamError("A2A stream response id mismatch")
            if frame.get("error"):
                raise RemoteStreamError("Remote A2A stream rejected request")
            payload = frame.get("result", {})
            if not isinstance(payload, dict):
                raise RemoteStreamError("Invalid A2A result")
            if "task" in payload:
                task = payload["task"]
                if not isinstance(task, dict):
                    raise RemoteStreamError("Invalid A2A task")
                status = task.get("status", {}).get("state")
                if status in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"):
                    raise RemoteStreamError("Remote A2A task failed")
                if status == "TASK_STATE_COMPLETED":
                    artifacts = task.get("artifacts", [])
                    if artifacts:
                        canonical_output = parts_value(artifacts[-1].get("parts", []))
                    complete = True
            elif "artifactUpdate" in payload:
                update = payload["artifactUpdate"]
                if not isinstance(update, dict):
                    raise RemoteStreamError("Invalid A2A artifact update")
                chunk = parts_value(update.get("artifact", {}).get("parts", []))
                if update.get("lastChunk") and not update.get("append"):
                    canonical_output = chunk
                    continue
                if not isinstance(chunk, str):
                    continue
                if len(chunk.encode("utf-8")) > 4096:
                    raise RemoteStreamError("Remote A2A chunk exceeds 4096 bytes")
                cumulative += len(chunk.encode("utf-8"))
                if cumulative > 65536 or len(chunks) >= 256:
                    raise RemoteStreamError("Remote A2A stream quota exceeded")
                chunks.append(chunk)
                if on_delta and chunk:
                    await on_delta(chunk)
            elif "statusUpdate" in payload:
                status = payload["statusUpdate"].get("status", {}).get("state")
                if status in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"):
                    raise RemoteStreamError("Remote A2A task failed")
                if status == "TASK_STATE_COMPLETED":
                    complete = True
                if payload["statusUpdate"].get("final") and not complete:
                    raise RemoteStreamError("Remote A2A task terminated without success")
            elif "message" in payload:
                return parts_value(payload["message"].get("parts", []))
    if not complete:
        raise RemoteStreamError("Remote A2A stream ended before completion")
    output = canonical_output if canonical_output is not None else "".join(chunks)
    if len(json.dumps(output, ensure_ascii=False).encode("utf-8")) > 65536:
        raise RemoteStreamError("Remote A2A output quota exceeded")
    return output
