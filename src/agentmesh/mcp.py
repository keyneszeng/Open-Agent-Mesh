"""Limited MCP 2026-07-28 Streamable HTTP JSON-response tools bridge.

Not a complete MCP implementation: no SSE, sampling, resources, prompts or
multi-round trips. All calls share the same bearer ACL as the native API.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .store import public_task
from .workflows import WorkflowCreate
from .conversations import ConversationCreate, ConversationMessage, ConversationDiscussion

VERSION = "2026-07-28"


def _tool(name: str, description: str, properties: dict, required: list[str]):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}


TOOLS = [
    _tool("mesh_list_agents", "List agents accessible to the caller", {}, []),
    _tool("mesh_submit_task", "Submit a task to an allowed agent (human-gated agents require workflow approval)",
          {"to": {"type": "string"}, "input": {}, "idempotency_key": {"type": "string"}}, ["to", "input"]),
    _tool("mesh_get_task", "Read an owned or administratively visible task",
          {"task_id": {"type": "string"}}, ["task_id"]),
    _tool("mesh_create_workflow", "Create a persisted DAG workflow; approvals still require a separate human principal",
          {"steps": {"type": "array"}, "project_id": {"type": "string"},
           "idempotency_key": {"type": "string"}}, ["steps"]),
    _tool("mesh_get_workflow", "Inspect an owned workflow",
          {"workflow_id": {"type": "string"}}, ["workflow_id"]),
    _tool("mesh_create_conversation", "Open a scoped, finite-turn conversation with permitted agents",
          {"title": {"type": "string"}, "participants": {"type": "array", "items": {"type": "string"}},
           "project_id": {"type": "string"}, "max_turns": {"type": "integer"}}, ["title", "participants"]),
    _tool("mesh_get_conversation", "Get a visible conversation and its turn states",
          {"conversation_id": {"type": "string"}}, ["conversation_id"]),
    _tool("mesh_conversation_messages", "Read an authorized conversation transcript",
          {"conversation_id": {"type": "string"}, "after": {"type": "integer"}}, ["conversation_id"]),
    _tool("mesh_send_conversation_message", "Send a message to an authorized agent in a conversation",
          {"conversation_id": {"type": "string"}, "to": {"type": "string"}, "text": {"type": "string"},
           "idempotency_key": {"type": "string"}}, ["conversation_id", "to", "text"]),
    _tool("mesh_start_discussion", "Start an explicit, limited, rotating multi-agent discussion (human only)",
          {"conversation_id": {"type": "string"}, "prompt": {"type": "string"},
           "rounds": {"type": "integer"}, "idempotency_key": {"type": "string"}}, ["conversation_id", "prompt"]),
]


def _reply(id: Any, *, result: Any = None, error: dict | None = None):
    envelope = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        envelope["error"] = error
    else:
        envelope["result"] = result
    return envelope


def install_mcp_api(app, *, identity, registry, can_use, submit, owned, workflows, check_project, conversations):
    # A stateless JSON-response transport: send each message as a POST /mcp.
    @app.post("/mcp")
    async def mcp(request: Request, profile: dict = Depends(identity)):
        if len(await request.body()) > 256_000:
            raise HTTPException(413, "MCP message too large")
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _reply(None, error={"code": -32700, "message": "Parse error"})
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return _reply(None, error={"code": -32600, "message": "Invalid Request"})
        method = payload["method"]
        if request.headers.get("Mcp-Method") != method:
            return _reply(payload.get("id"), error={"code": -32600, "message": "Mcp-Method header mismatch"})
        if request.headers.get("MCP-Protocol-Version") != VERSION:
            return _reply(payload.get("id"), error={"code": -32600, "message": "Unsupported MCP version"})
        if method == "notifications/initialized" and "id" not in payload:
            return Response(status_code=202)
        if "id" not in payload or not isinstance(payload["id"], (str, int)) or isinstance(payload["id"], bool):
            return _reply(None, error={"code": -32600, "message": "Request ID required"})
        request_id = payload["id"]
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return _reply(request_id, error={"code": -32602, "message": "Invalid params"})
        if method == "initialize":
            if params.get("protocolVersion") != VERSION:
                return _reply(request_id, error={"code": -32602, "message": "Unsupported protocolVersion"})
            return _reply(request_id, result={"protocolVersion": VERSION, "capabilities": {"tools": {}},
                                               "serverInfo": {"name": "open-agent-mesh", "version": "0.4.0"}})
        if method == "ping":
            return _reply(request_id, result={})
        if method == "tools/list":
            if params.get("cursor"):
                return _reply(request_id, error={"code": -32602, "message": "Unknown cursor"})
            return _reply(request_id, result={"resultType": "complete", "tools": TOOLS})
        if method != "tools/call":
            return _reply(request_id, error={"code": -32601, "message": "Method not found"})
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or not any(t["name"] == name for t in TOOLS) or not isinstance(args, dict):
            return _reply(request_id, error={"code": -32602, "message": "Unknown tool or arguments"})
        if request.headers.get("Mcp-Name") != name:
            return _reply(request_id, error={"code": -32602, "message": "Mcp-Name header mismatch"})
        try:
            if name == "mesh_list_agents":
                if args:
                    raise ValueError("This tool takes no arguments")
                result = [{"id": spec.id, "skills": list(spec.skills), "requires_approval": spec.requires_approval}
                          for spec in registry.values() if can_use(profile, spec.id)]
            elif name == "mesh_submit_task":
                from .app import CreateTask
                if not {"to", "input"} <= args.keys() or set(args) - {"to", "input", "idempotency_key"}:
                    raise ValueError("Expected to, input and optional idempotency_key")
                task = submit(CreateTask.model_validate(args), profile)
                result = public_task(task)
            elif name == "mesh_get_task":
                if set(args) != {"task_id"} or not isinstance(args["task_id"], str):
                    raise ValueError("task_id required")
                result = public_task(owned(args["task_id"], profile))
            elif name == "mesh_create_workflow":
                if "steps" not in args or set(args) - {"steps", "project_id", "idempotency_key"}:
                    raise ValueError("Expected steps and optional project_id/idempotency_key")
                definition = WorkflowCreate.model_validate(args)
                if definition.project_id is not None:
                    check_project(definition.project_id, profile)
                for step in definition.steps:
                    if step.to not in registry:
                        raise HTTPException(404, "Agent not found")
                    if not can_use(profile, step.to):
                        raise HTTPException(403, "Agent not allowed")
                effective = [s.model_copy(update={"approval_required": True}) if registry[s.to].requires_approval else s
                             for s in definition.steps]
                definition = definition.model_copy(update={"steps": effective})
                result = workflows.create(profile["name"], definition)
            elif name == "mesh_get_workflow":
                if set(args) != {"workflow_id"} or not isinstance(args["workflow_id"], str):
                    raise ValueError("workflow_id required")
                result = workflows.get(args["workflow_id"])
                if result is None or (profile.get("role") != "admin" and result["owner"] != profile["name"]):
                    raise HTTPException(404, "Workflow not found")
            elif name == "mesh_create_conversation":
                if set(args) - {"title", "participants", "project_id", "max_turns"}:
                    raise ValueError("Invalid conversation arguments")
                definition = ConversationCreate.model_validate(args)
                if profile.get("agent_id"):
                    raise HTTPException(403, "Only humans may create conversations")
                if definition.project_id:
                    check_project(definition.project_id, profile, write=True)
                if any(a not in registry or not can_use(profile, a) or registry[a].requires_approval
                       for a in definition.participants):
                    raise HTTPException(403, "Agent not allowed")
                result = conversations.create(owner=profile["name"], title=definition.title,
                                              participants=definition.participants,
                                              project_id=definition.project_id, max_turns=definition.max_turns)
            elif name in ("mesh_get_conversation", "mesh_conversation_messages",
                          "mesh_send_conversation_message", "mesh_start_discussion"):
                if "conversation_id" not in args or not isinstance(args["conversation_id"], str):
                    raise ValueError("conversation_id required")
                cid = args["conversation_id"]
                conv = conversations.get(cid)
                if conv is None:
                    raise HTTPException(404, "Conversation not found")
                if conv["project_id"]:
                    check_project(conv["project_id"], profile)
                if (profile.get("role") != "admin" and conv["owner"] != profile["name"] and
                    profile.get("agent_id") not in conv["participants"]):
                    raise HTTPException(404, "Conversation not found")
                if name == "mesh_get_conversation":
                    if set(args) != {"conversation_id"}:
                        raise ValueError("Unexpected conversation arguments")
                    result = conv
                elif name == "mesh_conversation_messages":
                    if set(args) - {"conversation_id", "after"}:
                        raise ValueError("Unexpected message arguments")
                    after = args.get("after", 0)
                    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                        raise ValueError("Invalid after cursor")
                    result = conversations.messages(cid, after=after)
                elif name == "mesh_send_conversation_message":
                    if set(args) - {"conversation_id", "to", "text", "idempotency_key"}:
                        raise ValueError("Unexpected message arguments")
                    message = ConversationMessage.model_validate({k:v for k,v in args.items() if k != "conversation_id"})
                    if (message.to not in registry or message.to not in conv["participants"] or
                        not can_use(profile, message.to) or registry[message.to].requires_approval or
                        profile.get("agent_id") == message.to):
                        raise HTTPException(403, "Agent not allowed")
                    sender = f"agent:{profile['agent_id']}" if profile.get("agent_id") else f"user:{profile['name']}"
                    result = conversations.post(cid, sender=sender, to=message.to, text=message.text,
                                                request_key=message.idempotency_key)
                else:
                    if set(args) - {"conversation_id", "prompt", "rounds", "idempotency_key"}:
                        raise ValueError("Unexpected discussion arguments")
                    definition = ConversationDiscussion.model_validate({k:v for k,v in args.items() if k != "conversation_id"})
                    if profile.get("agent_id") or len(conv["participants"]) < 2:
                        raise HTTPException(403, "Human discussion with 2+ agents required")
                    if any(not can_use(profile, a) or registry[a].requires_approval for a in conv["participants"]):
                        raise HTTPException(403, "Agent not allowed")
                    result = conversations.post(cid, sender=f"user:{profile['name']}",
                                                to=conv["participants"][0], text=definition.prompt,
                                                auto_rounds=definition.rounds,
                                                request_key=definition.idempotency_key)
            else:
                raise ValueError("Unsupported tool")
        except (HTTPException, ValidationError, ValueError, LookupError, TypeError) as exc:
            # Do not echo sensitive arguments or exception contents to a remote LLM.
            if isinstance(exc, HTTPException):
                message = "Access denied or resource not found"
            elif isinstance(exc, LookupError):
                message = "Referenced project fact not found"
            else:
                message = "Invalid tool arguments or workflow conflict"
            return _reply(request_id, result={"isError": True, "content": [{"type": "text", "text": message}]})
        return _reply(request_id, result={"isError": False,
                                          "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                                          "structuredContent": {"data": result} if not isinstance(result, dict) else result})
