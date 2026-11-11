from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AgentSpec:
    id: str
    kind: str
    name: str
    description: str
    skills: tuple[str, ...]
    handler: str = ""
    url: str = ""
    token_env: str = ""
    timeout_seconds: int = 25
    requires_approval: bool = False


def load_agents(path: str) -> dict[str, AgentSpec]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("agents")
    if not isinstance(items, list) or not items:
        raise ValueError("Agent configuration requires a non-empty agents array")
    specs: dict[str, AgentSpec] = {}
    for item in items:
        agent_id = item.get("id", "")
        if not isinstance(agent_id, str) or not agent_id or not all(c.isalnum() or c in "-_" for c in agent_id):
            raise ValueError("agent id must contain only letters, numbers, hyphens, underscores")
        kind = item.get("kind")
        if kind not in ("python", "http_json", "http_sse", "a2a_jsonrpc", "a2a_stream"):
            raise ValueError(f"unsupported agent kind: {kind}")
        handler = item.get("handler", "")
        url = item.get("url", "")
        if kind == "python" and ":" not in handler:
            raise ValueError(f"{agent_id} needs a python module:function handler")
        if kind != "python":
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{agent_id} needs a valid configured HTTP(S) URL without credentials")
            # No self-registration endpoint exists: outbound destinations are admin-configured.
        if agent_id in specs:
            raise ValueError(f"duplicate agent id: {agent_id}")
        specs[agent_id] = AgentSpec(
            id=agent_id, kind=kind, name=item.get("name", agent_id),
            description=item.get("description", ""), skills=tuple(item.get("skills", [])),
            handler=handler, url=url, token_env=item.get("token_env", ""),
            timeout_seconds=int(item.get("timeout_seconds", 25)),
            requires_approval=item.get("requires_approval", False),
        )
        if not isinstance(specs[agent_id].requires_approval, bool):
            raise ValueError("requires_approval must be boolean")
        if not 1 <= specs[agent_id].timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be 1..300")
    return specs


def load_tokens() -> dict:
    raw = os.environ.get("MESH_TOKENS")
    if not raw:
        raise ValueError("MESH_TOKENS must be configured. No implicit/demo token is enabled.")
    data = json.loads(raw)
    if not isinstance(data, dict) or not data:
        raise ValueError("MESH_TOKENS must be a non-empty JSON object")
    seen_names = set()
    for token, profile in data.items():
        if len(token) < 12 or not isinstance(profile, dict) or not profile.get("name"):
            raise ValueError("tokens need >=12 characters and a profile with name")
        if profile["name"] in seen_names:
            raise ValueError("principal names must be unique across tokens to protect task ownership")
        seen_names.add(profile["name"])
        if not isinstance(profile.get("allowed_agents", []), list):
            raise ValueError("allowed_agents must be a list")
        if not isinstance(profile.get("can_approve", False), bool):
            raise ValueError("can_approve must be boolean")
        if not isinstance(profile.get("allowed_projects", []), list):
            raise ValueError("allowed_projects must be a list")
        agent_id = profile.get("agent_id")
        if agent_id is not None and (not isinstance(agent_id, str) or not agent_id or profile.get("role") == "admin"):
            raise ValueError("agent_id requires a non-admin agent principal")
        roles = profile.get("project_roles", {})
        if not isinstance(roles, dict) or any(v not in ("reader", "editor") for v in roles.values()):
            raise ValueError("project_roles must map project IDs to reader or editor")
    return data
