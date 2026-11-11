import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmesh.app import create_app
from agentmesh.runner import _a2a_output


@pytest.fixture
def client(tmp_path):
    config = Path(__file__).parent.parent / "agents.example.json"
    app = create_app(config_path=str(config), db_path=str(tmp_path / "mesh.sqlite3"), tokens={
        "secret-admin-12345": {"name": "admin", "role": "admin", "allowed_agents": ["*"]},
        "secret-agent-12345": {"name": "alice", "allowed_agents": ["echo", "uppercase"], "allowed_projects": ["demo"]},
        "secret-other-12345": {"name": "bob", "allowed_agents": ["echo"], "allowed_projects": ["demo"]},
    }, public_url="http://localhost:8765", max_depth=2)
    with TestClient(app) as tc:
        yield tc


def headers(token="secret-agent-12345", a2a=False):
    result = {"Authorization": f"Bearer {token}"}
    if a2a:
        result["A2A-Version"] = "1.0"
    return result


def wait(client, id, token="secret-agent-12345"):
    for _ in range(200):
        response = client.get(f"/v1/tasks/{id}", headers=headers(token))
        if response.status_code == 200 and response.json()["state"] in ("completed", "failed", "canceled"):
            return response.json()
        time.sleep(.02)
    raise AssertionError("task did not finish")


def test_agent_catalog_filters_acl(client):
    r = client.get("/v1/agents", headers=headers("secret-other-12345"))
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == ["echo"]


def test_no_auth(client):
    assert client.post("/v1/tasks", json={"to": "echo", "input": "hi"}).status_code == 401
    assert client.get("/v1/agents").status_code == 401


def test_bad_auth(client):
    assert client.get("/v1/agents", headers=headers("not-valid-key")).status_code == 401


def test_submit_complete(client):
    r = client.post("/v1/tasks", headers=headers(), json={"to": "uppercase", "input": "hello Agent"})
    assert r.status_code == 202
    result = wait(client, r.json()["id"])
    assert result["state"] == "completed"
    assert result["output"] == "HELLO AGENT"
    ev = client.get(f"/v1/tasks/{result['id']}/events", headers=headers()).json()
    assert [e["state"] for e in ev] == ["submitted", "working", "completed"]


def test_structured_input(client):
    r = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": {"name": "测试", "ok": True}})
    result = wait(client, r.json()["id"])
    assert result["output"] == {"name": "测试", "ok": True}


def test_deny_unapproved_agent(client):
    r = client.post("/v1/tasks", headers=headers("secret-other-12345"), json={"to": "uppercase", "input": "hey"})
    assert r.status_code == 403


def test_task_scoping(client):
    r = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "confidential"})
    id = r.json()["id"]
    assert client.get(f"/v1/tasks/{id}", headers=headers("secret-other-12345")).status_code == 404
    assert client.get(f"/v1/tasks/{id}/events", headers=headers("secret-other-12345")).status_code == 404
    assert client.get(f"/v1/tasks/{id}", headers=headers("secret-admin-12345")).status_code == 200


def test_idempotency_reuse(client):
    payload = {"to": "echo", "input": "abc", "idempotency_key": "task-one"}
    first = client.post("/v1/tasks", headers=headers(), json=payload)
    again = client.post("/v1/tasks", headers=headers(), json=payload)
    assert first.json()["id"] == again.json()["id"]
    conflict = client.post("/v1/tasks", headers=headers(), json={**payload, "input": "different"})
    assert conflict.status_code == 409


def test_delegation_limit(client):
    first = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "0"}).json()
    second = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "1", "parent_task_id": first["id"]}).json()
    third = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "2", "parent_task_id": second["id"]}).json()
    denied = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "3", "parent_task_id": third["id"]})
    assert denied.status_code == 409
    assert third["depth"] == 2


def test_cross_owner_parent_denied(client):
    first = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "0"}).json()
    denied = client.post("/v1/tasks", headers=headers("secret-other-12345"), json={"to": "echo", "input": "1", "parent_task_id": first["id"]})
    assert denied.status_code == 404


def test_card_is_public_and_versioned(client):
    res = client.get("/agents/echo/.well-known/agent-card.json")
    card = res.json()
    assert res.status_code == 200
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a/echo")
    assert card["security"] == [{"bearer": []}]
    assert card["capabilities"]["streaming"] is False


def rpc(method, params, id=7):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def test_a2a_send_then_get(client):
    data = rpc("SendMessage", {"message": {"messageId": "test-message-1", "role": "ROLE_USER",
                             "parts": [{"text": "hello mesh"}]}, "configuration": {"returnImmediately": True}})
    res = client.post("/a2a/uppercase", headers=headers(a2a=True), json=data)
    assert res.status_code == 200
    task = res.json()["result"]["task"]
    assert task["id"]
    assert task["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED")
    wait(client, task["id"])
    polled = client.post("/a2a/uppercase", headers=headers(a2a=True), json=rpc("GetTask", {"id": task["id"]})).json()
    assert polled["result"]["artifacts"][0]["parts"][0]["text"] == "HELLO MESH"


def test_a2a_default_blocks(client):
    data = rpc("SendMessage", {"message": {"messageId": "test-message-2", "role": "ROLE_USER", "parts": [{"text": "abc"}]}})
    r = client.post("/a2a/uppercase", headers=headers(a2a=True), json=data)
    assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_a2a_target_route(client):
    data = rpc("SendMessage", {"metadata": {"meshTarget": "echo"},
        "message": {"messageId": "test-message-3", "role": "ROLE_USER", "parts": [{"data": {"value": 7}}]},
        "configuration": {"returnImmediately": False}})
    r = client.post("/a2a", headers=headers(a2a=True), json=data).json()
    assert r["result"]["task"]["artifacts"][0]["parts"][0]["data"] == {"value": 7}


def test_a2a_idempotency(client):
    data = rpc("SendMessage", {"message": {"messageId": "test-message-4", "role": "ROLE_USER", "parts": [{"text": "hello"}]},
        "configuration": {"returnImmediately": True}})
    first = client.post("/a2a/echo", headers=headers(a2a=True), json=data).json()
    second = client.post("/a2a/echo", headers=headers(a2a=True), json=data).json()
    assert first["result"]["task"]["id"] == second["result"]["task"]["id"]


def test_a2a_version_mismatch(client):
    r = client.post("/a2a/echo", headers=headers(), json=rpc("GetTask", {"id": "unused"}))
    assert r.status_code == 400


def test_a2a_invalid_method(client):
    r = client.post("/a2a/echo", headers=headers(a2a=True), json=rpc("NotSupported", {}))
    assert r.json()["error"]["code"] == -32601


def test_a2a_result_parser():
    assert _a2a_output({"result": {"message": {"parts": [{"text": "ok"}]}}}) == (True, "ok", None)
    assert _a2a_output({"result": {"task": {"id": "remote-1", "status": {"state": "TASK_STATE_WORKING"}}}}) == (False, "remote-1", None)
    assert _a2a_output({"result": {"task": {"id": "remote-1", "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"data": {"answer": 42}}]}]}}}) == (True, {"answer": 42}, None)


def test_payload_limit(client):
    r = client.post("/v1/tasks", headers=headers(), json={"to": "echo", "input": "x" * 130_000})
    assert r.status_code == 413


def test_failure_is_recorded(client):
    r = client.post("/v1/tasks", headers=headers(), json={"to": "uppercase", "input": {"invalid": True}})
    result = wait(client, r.json()["id"])
    assert result["state"] == "failed"
    assert "expects a text input" not in result["error"]  # do not expose exception data


def test_shared_project_fact_across_principals(client):
    path = "/v1/projects/demo/facts/business-rule"
    created = client.put(path, headers=headers(), json={"value": {"rule": "核销后请付款"}, "expected_revision": 0})
    assert created.status_code == 200
    assert created.json()["revision"] == 1
    # Shared project knowledge is visible to specifically authorized other agent principals.
    read = client.get(path, headers=headers("secret-other-12345"))
    assert read.status_code == 200
    assert read.json()["value"]["rule"] == "核销后请付款"
    update = client.put(path, headers=headers("secret-other-12345"), json={"value": "new rule", "expected_revision": 1})
    assert update.json()["revision"] == 2
    history = client.get(path + "/history", headers=headers()).json()
    assert [entry["revision"] for entry in history] == [1, 2]


def test_project_fact_version_conflict(client):
    path = "/v1/projects/demo/facts/architecture"
    assert client.put(path, headers=headers(), json={"value": "v1", "expected_revision": 0}).status_code == 200
    stale = client.put(path, headers=headers(), json={"value": "overwritten", "expected_revision": 0})
    assert stale.status_code == 409
    assert client.get(path, headers=headers()).json()["value"] == "v1"


def test_project_scope_denied(client):
    assert client.get("/v1/projects/private/facts", headers=headers()).status_code == 403
    assert client.put("/v1/projects/private/facts/foo", headers=headers(),
                       json={"value": "private", "expected_revision": 0}).status_code == 403


def test_project_fact_limit_and_invalid_id(client):
    assert client.put("/v1/projects/demo/facts/invalid%20key", headers=headers(),
                      json={"value": "hi", "expected_revision": 0}).status_code == 400
    assert client.put("/v1/projects/demo/facts/large", headers=headers(),
                      json={"value": "x" * 33_000, "expected_revision": 0}).status_code == 409
