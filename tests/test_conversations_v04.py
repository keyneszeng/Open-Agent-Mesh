"""Conversation Engine v0.4: durable history, multiple agents, ACL, safety, and faults."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmesh.app import create_app
from agentmesh.conversations import ConversationEngine
from agentmesh.store import Store


@pytest.fixture
def setup(tmp_path):
    agents = {"agents": [
        {"id": "echo", "kind": "python", "handler": "agentmesh.builtins:echo"},
        {"id": "upper", "kind": "python", "handler": "agentmesh.builtins:uppercase"},
        {"id": "probe", "kind": "python", "handler": "agentmesh.builtins:conversation_probe"},
        {"id": "broken", "kind": "python", "handler": "agentmesh.builtins:fail_demo"},
        {"id": "danger", "kind": "python", "handler": "agentmesh.builtins:echo", "requires_approval": True},
    ]}
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps(agents))
    tokens = {
        "alice-token-000000": {"name": "alice", "allowed_agents": ["echo", "upper", "probe", "broken", "danger"],
                               "project_roles": {"p1": "editor"}},
        "bob-token-00000000": {"name": "bob", "allowed_agents": ["echo", "upper", "probe"],
                                "project_roles": {"p1": "reader"}},
        "guest-token-000000": {"name": "guest", "allowed_agents": ["echo"],
                               "project_roles": {"p2": "editor"}},
        "agent-token-000000": {"name": "auto-upper", "agent_id": "upper",
                               "allowed_agents": ["echo"], "project_roles": {"p1": "reader"}},
        "admin-token-000000": {"name": "admin", "role": "admin", "allowed_agents": ["*"]},
    }
    app = create_app(config_path=str(cfg), db_path=str(tmp_path / "mesh.db"), tokens=tokens)
    with TestClient(app) as client:
        yield client, app


def auth(who="alice"):
    return {"Authorization": "Bearer " + {
        "alice": "alice-token-000000", "bob": "bob-token-00000000",
        "guest": "guest-token-000000", "agent": "agent-token-000000",
        "admin": "admin-token-000000",
    }[who]}


def new(client, *, participants=None, project_id=None, max_turns=12):
    response = client.post("/v1/conversations", headers=auth(), json={
        "title": "Review architecture", "participants": participants or ["echo", "upper"],
        "project_id": project_id, "max_turns": max_turns,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def done(client, cid, n, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = client.get(f"/v1/conversations/{cid}", headers=auth()).json()
        if len(value["turns"]) >= n and all(t["state"] in ("completed", "failed", "canceled") for t in value["turns"][:n]):
            return value
        time.sleep(.025)
    raise AssertionError("Conversation did not finish")


def test_manual_multi_turn_history_and_independent_agents(setup):
    client, _ = setup
    cid = new(client, participants=["upper", "probe"], project_id="p1")
    first = client.post(f"/v1/conversations/{cid}/messages", headers=auth(), json={"to": "upper", "text": "hello"})
    assert first.status_code == 202, first.text
    assert done(client, cid, 1)["turns"][0]["state"] == "completed"
    second = client.post(f"/v1/conversations/{cid}/messages", headers=auth(), json={"to": "probe", "text": "Now review"})
    assert second.status_code == 202
    final = done(client, cid, 2)
    assert final["requested_turns"] == 2
    messages = client.get(f"/v1/conversations/{cid}/messages", headers=auth()).json()
    assert [m["sender"] for m in messages] == ["user:alice", "agent:upper", "user:alice", "agent:probe"]
    assert messages[1]["content"] == "HELLO"
    data = json.loads(messages[3]["content"])
    assert data["received"] == "Now review"
    assert data["conversation_id"] == cid
    assert data["previous_speakers"] == ["user:alice", "agent:upper", "user:alice"]
    assert [m["seq"] for m in client.get(f"/v1/conversations/{cid}/messages?after=2&limit=2", headers=auth()).json()] == [3, 4]
    events = client.get(f"/v1/conversations/{cid}/events", headers=auth()).json()
    assert any(x["kind"] == "completed" for x in events)


def test_bounded_autonomous_discussion_no_infinite_ping_pong(setup):
    client, _ = setup
    cid = new(client, participants=["upper", "echo"], max_turns=4)
    response = client.post(f"/v1/conversations/{cid}/discussions", headers=auth(),
                           json={"prompt": "collaborate", "rounds": 4, "idempotency_key": "debate-001"})
    assert response.status_code == 202, response.text
    complete = done(client, cid, 4)
    assert complete["auto_remaining"] == 0
    assert complete["requested_turns"] == 4
    assert [t["agent_id"] for t in complete["turns"]] == ["upper", "echo", "upper", "echo"]
    messages = client.get(f"/v1/conversations/{cid}/messages", headers=auth()).json()
    assert len(messages) == 5
    assert [m["content"] for m in messages] == ["collaborate", "COLLABORATE", "COLLABORATE", "COLLABORATE", "COLLABORATE"]
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json={"to": "echo", "text": "extra"}).status_code == 409
    replay = client.post(f"/v1/conversations/{cid}/discussions", headers=auth(),
                         json={"prompt": "collaborate", "rounds": 4, "idempotency_key": "debate-001"})
    assert replay.status_code == 202 and replay.json()["replayed"]


def test_failures_stop_auto_relay(setup):
    client, _ = setup
    cid = new(client, participants=["upper", "broken"], max_turns=10)
    response = client.post(f"/v1/conversations/{cid}/discussions", headers=auth(),
                           json={"prompt": "okay", "rounds": 5})
    assert response.status_code == 202
    result = done(client, cid, 2)
    assert [turn["state"] for turn in result["turns"]] == ["completed", "failed"]
    assert result["auto_remaining"] == 0
    assert len(client.get(f"/v1/conversations/{cid}/messages", headers=auth()).json()) == 2
    assert not any("Simulated agent failure" in e["detail"] for e in
                   client.get(f"/v1/conversations/{cid}/events", headers=auth()).json())


def test_permissions_prevent_spoofing_leakage_and_approval_bypass(setup):
    client, _ = setup
    cid = new(client, participants=["upper", "echo"], project_id="p1")
    assert client.get(f"/v1/conversations/{cid}", headers=auth("bob")).status_code == 404
    assert client.get(f"/v1/conversations/{cid}", headers=auth("guest")).status_code == 403
    assert client.get(f"/v1/conversations/{cid}", headers=auth("agent")).status_code == 200
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth("agent"),
                       json={"to": "upper", "text": "self"}).status_code == 403
    assert client.post(f"/v1/conversations/{cid}/discussions", headers=auth("agent"),
                       json={"prompt": "secret", "rounds": 3}).status_code == 403
    assert client.post("/v1/conversations", headers=auth("agent"),
                       json={"title": "fake", "participants": ["echo"]}).status_code == 403
    assert client.post("/v1/conversations", headers=auth(),
                       json={"title": "danger", "participants": ["danger"]}).status_code == 403
    assert client.post("/v1/conversations", headers=auth("bob"),
                       json={"title": "reader", "project_id": "p1", "participants": ["echo"]}).status_code == 403
    # Agent-to-agent message attributed to actual credential; cannot impersonate another agent.
    r = client.post(f"/v1/conversations/{cid}/messages", headers=auth("agent"),
                    json={"to": "echo", "text": "review the change"})
    assert r.status_code == 202
    done(client, cid, 1)
    assert client.get(f"/v1/conversations/{cid}/messages", headers=auth()).json()[0]["sender"] == "agent:upper"
    assert client.post(f"/v1/conversations/{cid}/close", headers=auth("agent")).status_code == 403


def test_idempotency_budget_and_input_boundaries(setup):
    client, _ = setup
    cid = new(client, participants=["echo"], max_turns=2)
    payload = {"to": "echo", "text": "ping", "idempotency_key": "key1"}
    result = client.post(f"/v1/conversations/{cid}/messages", headers=auth(), json=payload)
    assert result.status_code == 202
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json={"to": "echo", "text": "next"}).status_code == 409
    done(client, cid, 1)
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(), json=payload).json()["replayed"]
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json=payload | {"text": "different"}).status_code == 409
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json={"to": "echo", "text": "🙂" * 2000}).status_code == 409
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json={"to": "echo", "text": "round two"}).status_code == 202
    done(client, cid, 2)
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json={"to": "echo", "text": "round three"}).status_code == 409


def test_close_cancels_pending_and_rejects_followups(setup):
    client, app = setup
    cid = new(client)
    client.post(f"/v1/conversations/{cid}/messages", headers=auth(), json={"to": "echo", "text": "hello"})
    close = client.post(f"/v1/conversations/{cid}/close", headers=auth())
    assert close.status_code == 200
    assert close.json()["state"] == "closed"
    assert client.post(f"/v1/conversations/{cid}/messages", headers=auth(),
                       json={"to": "echo", "text": "again"}).status_code == 409
    app.state.conversations.tick()
    assert all(x["state"] == "canceled" for x in app.state.conversations.get(cid)["turns"])


def test_sqlite_restart_resume_and_double_tick_idempotency(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    engine = ConversationEngine(store)
    cid = engine.create(owner="alice", title="Resume", participants=["echo", "probe"],
                        project_id=None, max_turns=3)["id"]
    engine.post(cid, sender="user:alice", to="echo", text="recover", auto_rounds=3)
    resumed = ConversationEngine(Store(store.path))
    resumed.tick()
    task = resumed.store.get(resumed.get(cid)["turns"][0]["task_id"])
    assert task["state"] == "submitted"
    claimed = resumed.store.claim()
    assert resumed.store.finish(claimed["id"], "completed", output="done", lease_token=claimed["lease_token"])
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: ConversationEngine(Store(store.path)).tick(), range(5)))
    state = resumed.get(cid)
    assert len(state["turns"]) == 2
    assert state["turns"][1]["state"] in ("queued", "submitted")
    assert [m["sender"] for m in resumed.messages(cid)] == ["user:alice", "agent:echo"]
    resumed.tick()
    pending = resumed.get(cid)["turns"][1]["task_id"]
    if pending:
        assert len(resumed.store.list(owner="alice")) == 2


def test_concurrent_same_idempotency_key_single_turn(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    engine = ConversationEngine(store)
    cid = engine.create(owner="alice", title="Concurrency", participants=["echo"], project_id=None, max_turns=5)["id"]
    def send(_):
        return engine.post(cid, sender="user:alice", to="echo", text="same", request_key="stable")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(send, range(40)))
    assert {r["message_seq"] for r in results} == {1}
    assert engine.get(cid)["requested_turns"] == 1
    assert len(engine.messages(cid)) == 1
    assert sum(not r["replayed"] for r in results) == 1


def test_transcript_persists_across_engine_recreation(setup):
    client, app = setup
    cid = new(client, participants=["echo"])
    client.post(f"/v1/conversations/{cid}/messages", headers=auth(), json={"to": "echo", "text": "durable"})
    done(client, cid, 1)
    reopened = ConversationEngine(Store(app.state.store.path))
    assert reopened.get(cid)["turns"][0]["state"] == "completed"
    assert len(reopened.messages(cid)) == 2
    assert reopened.messages(cid)[1]["content"] == "durable"


def test_invalid_participants_discussion_and_page(setup):
    client, _ = setup
    assert client.post("/v1/conversations", headers=auth(), json={
        "title": "duplicates", "participants": ["echo", "echo"]}).status_code == 422
    cid = new(client, participants=["echo"], max_turns=2)
    assert client.post(f"/v1/conversations/{cid}/discussions", headers=auth(),
                       json={"prompt": "why", "rounds": 2}).status_code == 422
    assert client.get(f"/v1/conversations/{cid}/messages?limit=101", headers=auth()).status_code == 422
    assert client.get(f"/v1/conversations/{cid}/messages?after=-1", headers=auth()).status_code == 422


def test_a2a_context_continuity_and_idempotency(setup):
    client, app = setup
    headers = auth() | {"A2A-Version": "1.0"}
    def send(message_id, text, context_id=None, task_id=None):
        msg = {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": text}]}
        if context_id is not None:
            msg["contextId"] = context_id
        if task_id is not None:
            msg["taskId"] = task_id
        return client.post("/a2a/echo", headers=headers, json={
            "jsonrpc": "2.0", "id": 5, "method": "SendMessage",
            "params": {"message": msg, "configuration": {"returnImmediately": True}}}).json()
    first = send("hello-001", "start")
    assert "task" in first["result"]
    task1 = first["result"]["task"]
    context_id = task1["contextId"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and app.state.store.get(task1["id"])["state"] != "completed":
        time.sleep(.02)
    second = send("hello-002", "continue", context_id=context_id)
    task2 = second["result"]["task"]
    assert task2["contextId"] == context_id and task2["id"] != task1["id"]
    stored = app.state.store.get(task2["id"])
    assert json.loads(stored["context_json"])["_a2a_history"][0]["input"] == '"start"'
    repeated = send("hello-002", "continue", context_id=context_id)
    assert repeated["result"]["task"]["id"] == task2["id"]
    mismatch = send("hello-002", "hijacked", context_id=context_id)
    assert mismatch["error"]["code"] == -32602
    wrong_context = send("hello-003", "wrong", context_id="other-context", task_id=task1["id"])
    assert wrong_context["error"]["code"] == -32602
    followup_completed = send("hello-004", "no", context_id=context_id, task_id=task1["id"])
    assert followup_completed["error"]["code"] == -32602


def test_token_agent_id_validation(monkeypatch):
    from agentmesh.config import load_tokens
    monkeypatch.setenv("MESH_TOKENS", json.dumps({"this-is-valid1234": {
        "name": "fake", "role": "admin", "agent_id": "echo"}}))
    with pytest.raises(ValueError, match="non-admin"):
        load_tokens()


def _mcp(client, name, arguments, who="alice"):
    return client.post("/mcp", headers=auth(who) | {
        "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": name,
    }, json={"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}).json()["result"]


def test_mcp_conversation_tools_with_cross_principal_permissions(setup):
    client, _ = setup
    tools = client.post("/mcp", headers=auth() | {
        "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list",
    }, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}).json()["result"]["tools"]
    assert "mesh_start_discussion" in {tool["name"] for tool in tools}
    created = _mcp(client, "mesh_create_conversation", {"title": "MCP chat", "participants": ["upper", "echo"], "max_turns": 3})
    assert created["isError"] is False
    cid = created["structuredContent"]["id"]
    assert _mcp(client, "mesh_get_conversation", {"conversation_id": cid}, who="bob")["isError"]
    assert _mcp(client, "mesh_create_conversation", {"title": "forbidden", "participants": ["echo"]}, who="agent")["isError"]
    discussion = _mcp(client, "mesh_start_discussion", {"conversation_id": cid, "prompt": "mcp auto", "rounds": 3})
    assert discussion["isError"] is False, discussion
    done(client, cid, 3)
    transcript = _mcp(client, "mesh_conversation_messages", {"conversation_id": cid})
    assert transcript["isError"] is False and len(transcript["structuredContent"]["data"]) == 4
    assert _mcp(client, "mesh_send_conversation_message", {"conversation_id": cid, "to": "upper", "text": "extra"})["isError"]
    assert _mcp(client, "mesh_send_conversation_message", {"conversation_id": cid, "to": "danger", "text": "bypass"})["isError"]


def test_persisted_previously_permitted_agent_becomes_gated(tmp_path):
    store = Store(str(tmp_path / "mesh.db"))
    old = ConversationEngine(store)
    cid = old.create(owner="alice", title="gating update", participants=["echo"], project_id=None, max_turns=5)["id"]
    old.post(cid, sender="user:alice", to="echo", text="not approved")
    new = ConversationEngine(Store(store.path), blocked_agents={"echo"})
    new.tick()
    assert new.get(cid)["turns"][0]["state"] == "failed"
    assert new.get(cid)["turns"][0]["task_id"] is None
    assert not new.store.list("alice")
    assert any(x["kind"] == "blocked" for x in new.events(cid))
