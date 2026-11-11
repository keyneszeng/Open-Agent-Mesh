"""v0.3 durability, crash recovery, fencing, approvals, and protocol boundary tests."""
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmesh.app import create_app
from agentmesh.store import Store


@pytest.fixture
def env(tmp_path):
    data = json.loads((Path(__file__).resolve().parents[1] / "agents.example.json").read_text())
    data["agents"].append({"id": "gated", "kind": "python", "handler": "agentmesh.builtins:echo",
                           "name": "Human gated demo", "requires_approval": True})
    config = tmp_path / "agents.json"
    config.write_text(json.dumps(data))
    tokens = {
        "alice-token-xyz123": {"name": "alice", "allowed_agents": ["echo", "gated"],
                               "project_roles": {"p1": "editor"}},
        "review-token-xyz123": {"name": "reviewer", "allowed_agents": [],
                                "can_approve": True, "project_roles": {"p1": "editor"}},
        "outsider-token-123": {"name": "outsider", "can_approve": True,
                                "project_roles": {"p2": "editor"}},
        "admin-token-xyz123": {"name": "admin", "role": "admin", "allowed_agents": ["*"]},
    }
    app = create_app(config_path=str(config), db_path=str(tmp_path / "mesh.db"), tokens=tokens)
    with TestClient(app) as client:
        yield client, app


def head(who="alice"):
    token = {"alice": "alice-token-xyz123", "reviewer": "review-token-xyz123",
             "outsider": "outsider-token-123", "admin": "admin-token-xyz123"}[who]
    return {"Authorization": "Bearer " + token}


def poll(client, workflow_id):
    for _ in range(150):
        data = client.get("/v1/workflows/" + workflow_id, headers=head()).json()
        if data["state"] != "running":
            return data
        time.sleep(.03)
    raise AssertionError("workflow completion timeout")


def await_approval(client, workflow_id, step_id="gate"):
    for _ in range(100):
        result = client.get("/v1/workflows/" + workflow_id, headers=head()).json()
        target = next(step for step in result["steps"] if step["id"] == step_id)
        if target["state"] == "awaiting_approval":
            return result
        time.sleep(.03)
    raise AssertionError("step approval wait timeout")


def test_expiring_lease_fences_old_worker(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    row, _ = store.create(agent_id="echo", owner="alice", input_value="hello",
                          context={}, request_key=None, parent_id=None, max_depth=1)
    old = store.claim(lease_seconds=2)
    assert old["id"] == row["id"]
    second = Store(str(tmp_path / "db.sqlite"))
    assert second.claim(lease_seconds=2) is None, "second process must not steal active lease"
    assert not second.renew(old["id"], "invalid", lease_seconds=2)
    assert second.renew(old["id"], old["lease_token"], lease_seconds=2)
    with store.connect() as db:
        db.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (old["id"],))
    assert not store.renew(old["id"], old["lease_token"], lease_seconds=2)
    assert not store.finish(old["id"], "completed", "stale output", lease_token=old["lease_token"])
    new = second.claim(lease_seconds=2)
    assert new["id"] == old["id"] and new["lease_token"] != old["lease_token"]
    assert new["attempts"] == 2
    assert not store.finish(old["id"], "completed", "stale", lease_token=old["lease_token"])
    assert second.finish(new["id"], "completed", "fresh", lease_token=new["lease_token"])
    assert store.get(new["id"])["output_json"] == '"fresh"'
    events = second.events(row["id"])
    assert [e["state"] for e in events] == ["submitted", "working", "submitted", "working", "completed"]


def test_parallel_claims_unique_and_durable(tmp_path):
    path = str(tmp_path / "parallel.db")
    store = Store(path)
    for i in range(70):
        store.create(agent_id="echo", owner="a", input_value=i,
                     context={}, request_key=f"key{i}", parent_id=None, max_depth=1)
    def collect(_):
        local = Store(path)
        claimed = []
        while row := local.claim(lease_seconds=30):
            claimed.append(row["id"])
            assert local.finish(row["id"], "completed", output=1, lease_token=row["lease_token"])
        return claimed
    with ThreadPoolExecutor(max_workers=6) as pool:
        lists = list(pool.map(collect, range(6)))
    ids = [id for sub in lists for id in sub]
    assert len(ids) == len(set(ids)) == 70
    assert len(Store(path).list(limit=100)) == 70


def test_migration_from_v02_database(tmp_path):
    path = str(tmp_path / "old.db")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, agent_id TEXT, owner TEXT, request_key TEXT,
             request_hash TEXT,input_json TEXT,context_json TEXT,parent_id TEXT,depth INTEGER,state TEXT,
             output_json TEXT,error TEXT,attempts INTEGER,created_at TEXT,updated_at TEXT)""")
        db.execute("""INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   ("old", "echo", "alice", None, "hash", '"hello"', '{}', None, 0,
                    "working", None, None, 1, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    store = Store(path)
    assert store.get("old")["state"] == "working"  # no early steal on construction
    recovered = store.claim()
    assert recovered["id"] == "old" and recovered["attempts"] == 2


def test_human_approval_enforced_and_audited(env):
    client, app = env
    assert client.post("/v1/tasks", headers=head(), json={"to": "gated", "input": "blocked"}).status_code == 403
    workflow = client.post("/v1/workflows", headers=head(), json={"project_id": "p1", "steps": [
        {"id": "gate", "to": "gated", "input": "safe", "approval_required": False},
        {"id": "later", "to": "echo", "input": {"$from": "gate"}, "depends_on": ["gate"]}
    ]})
    assert workflow.status_code == 202, workflow.text
    id = workflow.json()["id"]
    pending = await_approval(client, id)
    assert pending["steps"][0]["approval_required"] is True
    assert pending["steps"][0]["attempts"] == 0
    assert not app.state.store.list("alice")
    base = f"/v1/workflows/{id}/steps/gate/approve"
    assert client.post(base, headers=head()).status_code == 403  # no self approval
    assert client.post(base, headers=head("outsider")).status_code == 403  # project ACL
    assert client.post(base, headers=head("reviewer")).status_code == 200
    assert client.post(base, headers=head("reviewer")).status_code == 409
    result = poll(client, id)
    assert result["state"] == "completed"
    assert result["steps"][0]["reviewed_by"] == "reviewer"
    assert result["steps"][1]["output"] == "safe"
    history = client.get(f"/v1/workflows/{id}/events", headers=head()).json()
    assert any(e["kind"] == "approved" and "reviewer" in e["detail"] for e in history)


def test_reject_blocks_descendants_and_restart(env):
    client, app = env
    created = client.post("/v1/workflows", headers=head(), json={"steps": [
        {"id": "gate", "to": "gated", "input": "do not run"},
        {"id": "next", "to": "echo", "input": "no", "depends_on": ["gate"]}
    ]}).json()
    id = created["id"]
    await_approval(client, id)
    # A second WorkflowEngine instance must retain the review queue.
    from agentmesh.workflows import WorkflowEngine
    assert WorkflowEngine(Store(app.state.store.path)).get(id)["steps"][0]["state"] == "awaiting_approval"
    r = client.post(f"/v1/workflows/{id}/steps/gate/reject", headers=head("admin"))
    assert r.status_code == 200
    result = poll(client, id)
    assert [x["state"] for x in result["steps"]] == ["failed", "skipped"]
    assert all(x["attempts"] == 0 for x in result["steps"])


def test_cancel_while_awaiting_approval_no_later_review(env):
    client, _ = env
    id = client.post("/v1/workflows", headers=head(), json={"steps": [
        {"id": "gate", "to": "gated", "input": "no"}
    ]}).json()["id"]
    await_approval(client, id)
    assert client.post(f"/v1/workflows/{id}/cancel", headers=head()).json()["state"] == "canceled"
    assert client.post(f"/v1/workflows/{id}/steps/gate/approve", headers=head("admin")).status_code == 409


def mcp(client, method, params=None, who="alice", version="2026-07-28", header_override=None):
    headers = head(who) | {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if method == "tools/call" and params and "name" in params:
        headers["Mcp-Name"] = params["name"]
    if header_override:
        headers.update(header_override)
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 42, "method": method,
                                                      "params": params or {}})


def test_mcp_handshake_discovery_and_calls(env):
    client, _ = env
    response = mcp(client, "initialize", {"protocolVersion": "2026-07-28",
                                          "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert response.json()["result"]["protocolVersion"] == "2026-07-28"
    result = mcp(client, "tools/list").json()["result"]
    assert result["resultType"] == "complete"
    assert any(x["name"] == "mesh_submit_task" for x in result["tools"])
    listing = mcp(client, "tools/call", {"name": "mesh_list_agents", "arguments": {}}).json()["result"]
    assert "echo" in [x["id"] for x in listing["structuredContent"]["data"]]
    submitted = mcp(client, "tools/call", {"name": "mesh_submit_task",
                                            "arguments": {"to": "echo", "input": {"hello": 2},
                                                          "idempotency_key": "same"}}).json()["result"]
    assert submitted["isError"] is False
    task_id = submitted["structuredContent"]["id"]
    repeated = mcp(client, "tools/call", {"name": "mesh_submit_task",
                                          "arguments": {"to": "echo", "input": {"hello": 2},
                                                        "idempotency_key": "same"}}).json()["result"]
    assert repeated["structuredContent"]["id"] == task_id
    assert mcp(client, "tools/call", {"name": "mesh_get_task",
                                      "arguments": {"task_id": task_id}}, who="outsider").json()["result"]["isError"] is True
    denied = mcp(client, "tools/call", {"name": "mesh_submit_task", "arguments": {"to": "gated", "input": "bad"}})
    assert denied.json()["result"]["isError"] is True
    workflow = mcp(client, "tools/call", {"name": "mesh_create_workflow", "arguments": {
        "steps": [{"id": "gate", "to": "gated", "input": "ok", "approval_required": False}]}}).json()["result"]
    assert workflow["structuredContent"]["steps"][0]["approval_required"] is True
    id = workflow["structuredContent"]["id"]
    assert mcp(client, "tools/call", {"name": "mesh_get_workflow",
                                     "arguments": {"workflow_id": id}}).json()["result"]["structuredContent"]["id"] == id


def test_mcp_auth_version_headers_and_input_rejection(env):
    client, _ = env
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert mcp(client, "ping", version="2025-06-18").json()["error"]["code"] == -32600
    assert mcp(client, "ping", header_override={"Mcp-Method": "tools/list"}).json()["error"]["code"] == -32600
    assert mcp(client, "tools/call", {"name": "mesh_list_agents", "arguments": {}},
               header_override={"Mcp-Name": "mesh_get_task"}).json()["error"]["code"] == -32602
    assert mcp(client, "not-a-method").json()["error"]["code"] == -32601
    assert mcp(client, "tools/call", {"name": "mesh_nonexistent"}).json()["error"]["code"] == -32602
    assert mcp(client, "tools/call", {"name": "mesh_get_task", "arguments": {"task_id": "invalid"}}).json()["result"]["isError"] is True
    assert client.post("/mcp", headers=head(), content="x" * 300_000).status_code == 413


def test_gated_agent_cannot_bypass_via_a2a(env):
    client, _ = env
    request = {"jsonrpc": "2.0", "id": 31, "method": "SendMessage",
               "params": {"message": {"role": "ROLE_USER", "messageId": "gated",
                                      "parts": [{"text": "execute"}]}}}
    headers = head() | {"A2A-Version": "1.0"}
    response = client.post("/a2a/gated", headers=headers, json=request).json()
    assert response["error"]["code"] == -32004
    assert "result" not in response


def test_health_exposes_scheduler_failure(env):
    client, app = env
    previous = app.state.worker_task
    class Stopped:
        def done(self): return True
    try:
        app.state.worker_task = Stopped()
        unhealthy = client.get("/health")
        assert unhealthy.status_code == 503
        assert unhealthy.json()["ok"] is False
    finally:
        app.state.worker_task = previous
