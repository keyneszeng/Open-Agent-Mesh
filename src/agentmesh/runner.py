"""Agent adapters. All target URLs and Python imports are admin-configured, never user-supplied."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import uuid
from typing import Any

import httpx

from .config import AgentSpec


class AgentExecutionError(Exception):
    pass


def _part(value: Any) -> dict:
    if isinstance(value, str):
        return {"text": value}
    return {"data": value}


def _result_from_parts(parts: list[dict]) -> Any:
    if not parts:
        return None
    values = [part.get("text") if "text" in part else part.get("data") for part in parts]
    return values[0] if len(values) == 1 else values


def _a2a_output(body: dict) -> tuple[bool, Any, str | None]:
    payload = body.get("result", {})
    if not isinstance(payload, dict):
        raise AgentExecutionError("Invalid A2A response")
    if "error" in body:
        raise AgentExecutionError("Remote A2A error")
    if "message" in payload:
        return True, _result_from_parts(payload["message"].get("parts", [])), None
    task = payload.get("task", payload)
    status = task.get("status", {})
    state = status.get("state")
    if state in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"):
        return True, None, state
    if state == "TASK_STATE_COMPLETED":
        artifacts = task.get("artifacts", [])
        parts = [p for artifact in artifacts for p in artifact.get("parts", [])]
        return True, _result_from_parts(parts), None
    if state in ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"):
        return True, None, f"Remote task paused: {state} (human intervention required)"
    return False, task.get("id"), None


async def run_agent(spec: AgentSpec, *, task_id: str, value: Any,
                    context: dict, parent_id: str | None) -> Any:
    if spec.kind == "python":
        module, symbol = spec.handler.rsplit(":", 1)
        fn = getattr(importlib.import_module(module), symbol)
        if inspect.iscoroutinefunction(fn):
            return await asyncio.wait_for(fn(value, context), timeout=spec.timeout_seconds)
        result = await asyncio.wait_for(asyncio.to_thread(fn, value, context), timeout=spec.timeout_seconds)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=spec.timeout_seconds)
        return result

    headers = {"Content-Type": "application/json"}
    if spec.token_env:
        token = os.environ.get(spec.token_env)
        if not token:
            raise AgentExecutionError(f"Missing outbound auth environment variable for {spec.id}")
        headers["Authorization"] = f"Bearer {token}"
    timeout = httpx.Timeout(float(spec.timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        if spec.kind == "http_json":
            headers["Idempotency-Key"] = task_id
            res = await client.post(spec.url, headers=headers, json={
                "task_id": task_id, "input": value, "context": context,
                "parent_task_id": parent_id,
            })
            res.raise_for_status()
            response = res.json()
            if not isinstance(response, dict) or "output" not in response:
                raise AgentExecutionError("HTTP adapter must return JSON with an output field")
            return response["output"]
        if spec.kind == "a2a_jsonrpc":
            headers["A2A-Version"] = "1.0"
            message_id = task_id  # stable id on retries
            rpc_id = str(uuid.uuid4())
            params = {"message": {"messageId": message_id, "role": "ROLE_USER",
                                  "parts": [_part(value)]},
                      "configuration": {"returnImmediately": False}}
            res = await client.post(spec.url, headers=headers, json={
                "jsonrpc": "2.0", "id": rpc_id, "method": "SendMessage", "params": params,
            })
            res.raise_for_status()
            body = res.json()
            if body.get("error"):
                raise AgentExecutionError(f"Remote A2A error {body['error'].get('code', '')}")
            completed, output, error = _a2a_output(body)
            if error:
                raise AgentExecutionError(error)
            if completed:
                return output
            remote_task_id = output
            if not remote_task_id:
                raise AgentExecutionError("Remote A2A response has no task id")
            # Continue polling for servers that choose to return a task immediately.
            for _ in range(60):
                await asyncio.sleep(0.3)
                poll = await client.post(spec.url, headers=headers, json={
                    "jsonrpc": "2.0", "id": str(uuid.uuid4()),
                    "method": "GetTask", "params": {"id": remote_task_id},
                })
                poll.raise_for_status()
                data = poll.json()
                if data.get("error"):
                    raise AgentExecutionError("Remote GetTask error")
                completed, output, error = _a2a_output(data)
                if error:
                    raise AgentExecutionError(error)
                if completed:
                    return output
            raise AgentExecutionError("Remote A2A task has not completed within polling limit")
    raise AgentExecutionError(f"unsupported adapter: {spec.kind}")
