"""HTTP task mesh + limited A2A 1.0 JSON-RPC binding (not a full A2A implementation)."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import AgentSpec, load_agents, load_tokens
from .runner import run_agent
from .store import Store, TERMINAL, public_task
from .workflows import WorkflowEngine, install_workflow_api


class CreateTask(BaseModel):
    to: str
    input: Any
    context: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
    parent_task_id: str | None = None


class FactChange(BaseModel):
    value: Any
    expected_revision: int = Field(ge=0)


STATES = {"submitted": "TASK_STATE_SUBMITTED", "working": "TASK_STATE_WORKING",
          "completed": "TASK_STATE_COMPLETED", "failed": "TASK_STATE_FAILED",
          "canceled": "TASK_STATE_CANCELED"}


def a2a_task(row: dict) -> dict:
    context = json.loads(row["context_json"])
    result = {"id": row["id"], "contextId": context.get("_a2a_context_id", row["id"]),
              "status": {"state": STATES[row["state"]], "timestamp": row["updated_at"]}}
    if row["state"] == "completed":
        output = json.loads(row["output_json"])
        part = {"text": output} if isinstance(output, str) else {"data": output}
        result["artifacts"] = [{"artifactId": "output", "name": "Agent result", "parts": [part]}]
    if row["state"] == "failed":
        result["status"]["message"] = {"messageId": str(uuid.uuid4()), "role": "ROLE_AGENT",
                                          "contextId": result["contextId"],
                                          "parts": [{"text": "Task failed; inspect task audit logs"}]}
    return result


def create_app(*, config_path: str | None = None, db_path: str | None = None,
               tokens: dict | None = None, public_url: str | None = None,
               max_depth: int = 5) -> FastAPI:
    path = config_path or os.environ.get("MESH_AGENTS_CONFIG", "agents.example.json")
    registry = load_agents(path)
    credentials = tokens if tokens is not None else load_tokens()
    if not credentials:
        raise ValueError("At least one explicit token is required")
    store = Store(db_path or os.environ.get("MESH_DB_PATH", "/tmp/agentmesh.sqlite3"))
    base = (public_url or os.environ.get("MESH_PUBLIC_URL", "http://127.0.0.1:8765")).rstrip("/")
    in_flight: set[asyncio.Task] = set()
    workflows = WorkflowEngine(store)

    async def execute(row):
        try:
            output = await run_agent(registry[row["agent_id"]], task_id=row["id"],
                                     value=json.loads(row["input_json"]),
                                     context=json.loads(row["context_json"]),
                                     parent_id=row["parent_id"])
            store.finish(row["id"], "completed", output=output)
        except asyncio.CancelledError:
            store.finish(row["id"], "failed", error="Worker interrupted; retry after service restart")
            raise
        except Exception as exc:
            # Avoid leaking exception internals, URLs or remote credentials to callers.
            store.finish(row["id"], "failed", error=f"{type(exc).__name__}: execution failed")

    async def worker():
        while True:
            workflows.tick()
            # Single-process async worker with bounded in-flight concurrency.
            while len(in_flight) < 4:
                row = store.claim()
                if row is None:
                    break
                task = asyncio.create_task(execute(row))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            await asyncio.sleep(0.06)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner = asyncio.create_task(worker())
        try:
            yield
        finally:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            for task in tuple(in_flight):
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    app = FastAPI(title="Open Agent Mesh", version="0.2.0", lifespan=lifespan)
    app.state.store = store
    app.state.workflows = workflows

    def identity(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization[7:]
        for key, profile in credentials.items():
            if hmac.compare_digest(supplied, key):
                return profile
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})

    def can_use(profile: dict, agent_id: str) -> bool:
        allowed = profile.get("allowed_agents", [])
        return profile.get("role") == "admin" or agent_id in allowed or "*" in allowed

    def owned(task_id: str, profile: dict, agent_id: str | None = None) -> dict:
        row = store.get(task_id)
        if not row or (profile.get("role") != "admin" and row["owner"] != profile["name"]):
            raise HTTPException(404, "Task not found")
        if agent_id and row["agent_id"] != agent_id:
            raise HTTPException(404, "Task not found")
        return row

    def submit(data: CreateTask, profile: dict):
        if data.to not in registry:
            raise HTTPException(404, "Agent not found")
        if not can_use(profile, data.to):
            raise HTTPException(403, "Agent not allowed")
        encoded = json.dumps({"input": data.input, "context": data.context}, ensure_ascii=False)
        if len(encoded.encode()) > 128_000:
            raise HTTPException(413, "Task payload exceeds 128 KB")
        try:
            row, _ = store.create(agent_id=data.to, owner=profile["name"],
                                  input_value=data.input, context=data.context,
                                  request_key=data.idempotency_key, parent_id=data.parent_task_id,
                                  max_depth=max_depth)
        except PermissionError:
            raise HTTPException(404, "Parent task not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return row

    def card(agent: AgentSpec | None = None) -> dict:
        if agent:
            agent_id = agent.id
            return {
                "name": agent.name, "description": agent.description,
                "supportedInterfaces": [{"url": f"{base}/a2a/{agent_id}",
                                         "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
                "version": "0.2.0", "capabilities": {"streaming": False, "pushNotifications": False},
                "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
                "security": [{"bearer": []}],
                "defaultInputModes": ["text/plain", "application/json"],
                "defaultOutputModes": ["text/plain", "application/json"],
                "skills": [{"id": x, "name": x, "description": agent.description, "tags": [x]}
                           for x in agent.skills] or [{"id": agent_id, "name": agent_id,
                                                        "description": agent.description, "tags": [agent_id]}],
            }
        return {
            "name": "Open Agent Mesh Router", "description": "Route to a configured agent using params.metadata.meshTarget",
            "supportedInterfaces": [{"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "version": "0.2.0", "capabilities": {"streaming": False, "pushNotifications": False},
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
            "security": [{"bearer": []}],
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "skills": [{"id": "mesh-routing", "name": "Route task", "description": "Route to approved registered agent", "tags": ["routing"]}],
        }

    @app.get("/health")
    async def health():
        return {"ok": True, "agents": len(registry)}

    @app.get("/.well-known/agent-card.json")
    async def root_card():
        return JSONResponse(card(), headers={"Cache-Control": "public,max-age=120"})

    @app.get("/agents/{agent_id}/.well-known/agent-card.json")
    async def one_card(agent_id: str):
        if agent_id not in registry:
            raise HTTPException(404, "Agent not found")
        return JSONResponse(card(registry[agent_id]), headers={"Cache-Control": "public,max-age=120"})

    @app.get("/v1/agents")
    async def agents(profile: dict = Depends(identity)):
        return [{"id": x.id, "name": x.name, "description": x.description, "skills": list(x.skills),
                 "a2a_card": f"{base}/agents/{x.id}/.well-known/agent-card.json"}
                for x in registry.values() if can_use(profile, x.id)]

    def check_project(project_id: str, profile: dict, key: str | None = None, write: bool = False):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", project_id):
            raise HTTPException(400, "Invalid project ID")
        if key is not None and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", key):
            raise HTTPException(400, "Invalid fact key")
        roles = profile.get("project_roles", {})
        role = roles.get(project_id)
        legacy_editor = project_id in profile.get("allowed_projects", [])
        if profile.get("role") != "admin" and not (legacy_editor or (role == "editor" if write else role in ("reader", "editor"))):
            raise HTTPException(403, "Project not allowed")

    @app.get("/v1/projects/{project_id}/facts")
    async def list_project_facts(project_id: str, profile: dict = Depends(identity)):
        check_project(project_id, profile)
        return store.list_facts(project_id)

    @app.get("/v1/projects/{project_id}/facts/{key}")
    async def get_project_fact(project_id: str, key: str, profile: dict = Depends(identity)):
        check_project(project_id, profile, key)
        fact = store.get_fact(project_id, key)
        if not fact:
            raise HTTPException(404, "Fact not found")
        return fact

    @app.put("/v1/projects/{project_id}/facts/{key}")
    async def put_project_fact(project_id: str, key: str, payload: FactChange, profile: dict = Depends(identity)):
        check_project(project_id, profile, key, write=True)
        try:
            return store.put_fact(project_id, key, payload.value, profile["name"], payload.expected_revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/v1/projects/{project_id}/facts/{key}/history")
    async def project_fact_history(project_id: str, key: str, profile: dict = Depends(identity)):
        check_project(project_id, profile, key)
        if not store.get_fact(project_id, key):
            raise HTTPException(404, "Fact not found")
        return store.fact_history(project_id, key)

    @app.post("/v1/tasks", status_code=202)
    async def create_task(data: CreateTask, profile: dict = Depends(identity)):
        return public_task(submit(data, profile))

    @app.get("/v1/tasks")
    async def list_tasks(profile: dict = Depends(identity)):
        owner = None if profile.get("role") == "admin" else profile["name"]
        return [public_task(row) for row in store.list(owner)]

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, profile: dict = Depends(identity)):
        return public_task(owned(task_id, profile))

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, profile: dict = Depends(identity)):
        owned(task_id, profile)
        return public_task(store.cancel(task_id))

    @app.get("/v1/tasks/{task_id}/events")
    async def events(task_id: str, profile: dict = Depends(identity)):
        owned(task_id, profile)
        return store.events(task_id)

    async def rpc(agent_id: str | None, request: Request, profile: dict):
        version = request.headers.get("A2A-Version")
        if version != "1.0":
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001,
                "message": "A2A-Version: 1.0 required; v0.3 is not supported"}}, status_code=400)
        try:
            data = await request.json()
        except Exception:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or not isinstance(data.get("method"), str) or "id" not in data:
            return {"jsonrpc": "2.0", "id": data.get("id") if isinstance(data, dict) else None,
                    "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        rpc_id, method = data["id"], data["method"]
        params = data.get("params", {})
        try:
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "SendMessage":
                metadata = params.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("metadata must be an object")
                target = agent_id or metadata.get("meshTarget")
                if not isinstance(target, str) or target not in registry:
                    raise ValueError("Known target agent required")
                message = params.get("message")
                if not isinstance(message, dict) or message.get("role") != "ROLE_USER" or not message.get("messageId"):
                    raise ValueError("message requires messageId and ROLE_USER")
                parts = message.get("parts", [])
                if not isinstance(parts, list) or not parts:
                    raise ValueError("message.parts required")
                values = []
                for part in parts:
                    if not isinstance(part, dict) or sum(k in part for k in ("text", "data", "raw", "url")) != 1:
                        raise ValueError("each part requires exactly one of text/data/raw/url")
                    if "text" in part:
                        if not isinstance(part["text"], str):
                            raise ValueError("text part must be a string")
                        values.append(part["text"])
                    elif "data" in part:
                        values.append(part["data"])
                    else:
                        raise ValueError("MVP supports text and data parts only")
                value = values[0] if len(values) == 1 else values
                if message.get("taskId"):
                    raise ValueError("Multi-turn messages to an existing task are not yet supported")
                context = {"_a2a_context_id": message.get("contextId") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{profile['name']}:{target}:{message['messageId']}"))}
                row = submit(CreateTask(to=target, input=value, context=context,
                                        idempotency_key=f"a2a:{target}:{message['messageId']}"), profile)
                configuration = params.get("configuration", {})
                if not isinstance(configuration, dict):
                    raise ValueError("configuration must be an object")
                immediate = configuration.get("returnImmediately", False)
                if not isinstance(immediate, bool):
                    raise ValueError("returnImmediately must be boolean")
                if not immediate:
                    # Spec default: block for terminal state. Agent timeouts are bounded.
                    while row["state"] not in TERMINAL:
                        await asyncio.sleep(.075)
                        row = store.get(row["id"])
                return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": a2a_task(row)}}
            if method in ("GetTask", "CancelTask"):
                if not isinstance(params.get("id"), str):
                    raise ValueError("Task id required")
                row = owned(params["id"], profile, agent_id)
                if method == "CancelTask":
                    row = store.cancel(row["id"])
                return {"jsonrpc": "2.0", "id": rpc_id, "result": a2a_task(row)}
            if method == "ListTasks":
                owner = None if profile.get("role") == "admin" else profile["name"]
                rows = store.list(owner, limit=min(100, max(1, int(params.get("pageSize", 50)))))
                if agent_id:
                    rows = [r for r in rows if r["agent_id"] == agent_id]
                if params.get("status"):
                    rows = [r for r in rows if STATES[r["state"]] == params["status"]]
                if params.get("contextId"):
                    rows = [r for r in rows if json.loads(r["context_json"]).get("_a2a_context_id") == params["contextId"]]
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "result": {"tasks": [a2a_task(r) for r in rows], "nextPageToken": ""}}
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "Method not implemented"}}
        except HTTPException as exc:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32004, "message": "Task not found or inaccessible" if exc.status_code == 404 else "Forbidden"}}
        except (ValueError, TypeError) as exc:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": str(exc)}}

    @app.post("/a2a")
    async def a2a_router(request: Request, profile: dict = Depends(identity)):
        return await rpc(None, request, profile)

    @app.post("/a2a/{agent_id}")
    async def a2a_agent(agent_id: str, request: Request, profile: dict = Depends(identity)):
        if agent_id not in registry:
            raise HTTPException(404, "Agent not found")
        if not can_use(profile, agent_id):
            raise HTTPException(403, "Agent not allowed")
        return await rpc(agent_id, request, profile)

    install_workflow_api(app, workflows, identity, registry, can_use, check_project)
    return app
