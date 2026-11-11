"""Real HTTP/API tests of persisted workflow scheduling, scope and retry."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmesh.app import create_app
from agentmesh.config import load_tokens
from agentmesh.store import Store
from agentmesh.workflows import WorkflowEngine


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    config = Path(__file__).parent.parent / "agents.example.json"
    extra = tmp_path / "test_handlers.py"
    extra.write_text("""\nn = 0\ndef flaky(value, context):\n    global n\n    n += 1\n    if n == 1:\n        raise ValueError('private failure details')\n    return value\ndef see_context(value, context):\n    return context\n""")
    monkeypatch.syspath_prepend(str(tmp_path))
    data = json.loads(config.read_text())
    data["agents"] += [
        {"id": "flaky", "kind": "python", "handler": "test_handlers:flaky"},
        {"id": "ctx", "kind": "python", "handler": "test_handlers:see_context"},
    ]
    config = tmp_path / "agents.json"
    config.write_text(json.dumps(data))
    tokens = {
        "admin-token-123456789": {"name": "admin", "role": "admin", "allowed_agents": ["*"]},
        "alice-token-123456789": {"name": "alice", "allowed_agents": ["echo", "uppercase", "flaky", "ctx"], "project_roles": {"demo": "editor"}},
        "bob-token-123456789": {"name": "bob", "allowed_agents": ["echo"], "project_roles": {"demo": "reader"}},
        "guest-token-123456789": {"name": "guest", "allowed_agents": ["echo"]},
    }
    app = create_app(config_path=str(config), db_path=str(tmp_path / "mesh.sqlite3"), tokens=tokens)
    with TestClient(app) as client:
        yield client, app


def h(token="alice-token-123456789"):
    return {"Authorization": "Bearer " + token}


def step(id, to="echo", value="ok", **kwargs):
    return {"id": id, "to": to, "input": value, **kwargs}


def submit(client, steps, **kwargs):
    return client.post("/v1/workflows", headers=h(), json={"steps": steps, **kwargs})


def await_workflow(client, id, timeout=8):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        result = client.get("/v1/workflows/" + id, headers=h()).json()
        if result["state"] in ("completed", "failed", "canceled"):
            return result
        time.sleep(.05)
    raise AssertionError("workflow did not complete")


def test_dependency_result_and_parallel(mesh):
    client, _ = mesh
    response = submit(client, [
        step("first", "uppercase", "hello"),
        step("independent", value={"ok": 1}),
        step("second", "echo", {"answer": {"$from": "first"}}, depends_on=["first"]),
    ])
    assert response.status_code == 202, response.text
    result = await_workflow(client, response.json()["id"])
    assert result["state"] == "completed"
    nodes = {s["id"]: s for s in result["steps"]}
    assert nodes["first"]["output"] == "HELLO"
    assert nodes["independent"]["output"] == {"ok": 1}
    assert nodes["second"]["output"] == {"answer": "HELLO"}
    assert all(s["attempts"] == 1 for s in nodes.values())
    events = client.get("/v1/workflows/" + result["id"] + "/events", headers=h()).json()
    assert events[0]["kind"] == "created"
    assert events[-1]["kind"] == "completed"


@pytest.mark.parametrize("nodes", [
    [step("x", depends_on=["x"])],
    [step("x", depends_on=["y"]), step("y", depends_on=["x"])],
    [step("x", depends_on=["missing"])],
    [step("x", {"$from": "missing"})],
    [step("x", value={"$from": "y"}), step("y")],
    [step("x"), step("x")],
])
def test_bad_dag(mesh, nodes):
    client, _ = mesh
    r = submit(client, nodes)
    assert r.status_code == 422, r.text


def test_acl_denies_unavailable_agents_and_projects(mesh):
    client, _ = mesh
    assert client.post("/v1/workflows", headers=h("bob-token-123456789"),
                       json={"steps": [step("a", "uppercase")]}).status_code == 403
    assert client.post("/v1/workflows", headers=h("guest-token-123456789"),
                       json={"project_id": "demo", "steps": [step("a")]}).status_code == 403
    created = submit(client, [step("a")]).json()["id"]
    for path in ["/v1/workflows/" + created, "/v1/workflows/" + created + "/events", "/v1/workflows/" + created + "/cancel"]:
        method = client.post if path.endswith("cancel") else client.get
        assert method(path, headers=h("bob-token-123456789")).status_code == 404
    assert client.get("/v1/workflows", headers=h("bob-token-123456789")).json() == []


def test_project_reader_cannot_write(mesh):
    client, _ = mesh
    path = "/v1/projects/demo/facts/decision"
    assert client.put(path, headers=h(), json={"expected_revision": 0, "value": "human approved"}).status_code == 200
    assert client.get(path, headers=h("bob-token-123456789")).status_code == 200
    assert client.put(path, headers=h("bob-token-123456789"),
                      json={"expected_revision": 1, "value": "overwrite"}).status_code == 403


def test_fact_snapshot_and_explicit_sharing(mesh):
    client, _ = mesh
    key = "/v1/projects/demo/facts/decision"
    client.put(key, headers=h(), json={"expected_revision": 0, "value": "original"})
    submitted = submit(client, [step("inspect", "ctx", fact_keys=["decision"])], project_id="demo")
    assert submitted.status_code == 202
    client.put(key, headers=h(), json={"expected_revision": 1, "value": "new"})
    result = await_workflow(client, submitted.json()["id"])
    facts = result["steps"][0]["output"]["mesh_workflow"]["facts"]
    assert facts["decision"] == {"value": "original", "revision": 1}
    second = submit(client, [step("no-facts", "ctx")], project_id="demo")
    assert await_workflow(client, second.json()["id"])["steps"][0]["output"]["mesh_workflow"]["facts"] == {}


def test_missing_fact_rejected(mesh):
    client, _ = mesh
    result = submit(client, [step("a", fact_keys=["not-exist"])], project_id="demo")
    assert result.status_code == 404


def test_retry_and_no_secret_error(mesh):
    client, _ = mesh
    response = submit(client, [step("one", "flaky", "abc", max_attempts=2)])
    result = await_workflow(client, response.json()["id"], timeout=8)
    assert result["state"] == "completed"
    assert result["steps"][0]["attempts"] == 2
    assert result["steps"][0]["output"] == "abc"
    events = client.get("/v1/workflows/" + result["id"] + "/events", headers=h()).json()
    assert "private failure details" not in json.dumps(events)


def test_failed_dependency_skips_descendants(mesh):
    client, _ = mesh
    workflow = submit(client, [
        step("invalid", "uppercase", {"not": "text"}),
        step("downstream", value={"$from": "invalid"}, depends_on=["invalid"]),
    ]).json()
    result = await_workflow(client, workflow["id"])
    assert result["state"] == "failed"
    assert [s["state"] for s in result["steps"]] == ["failed", "skipped"]
    assert result["steps"][1]["attempts"] == 0


def test_idempotency_and_cancel(mesh):
    client, _ = mesh
    data = {"steps": [step("a")], "idempotency_key": "stable-key"}
    first = client.post("/v1/workflows", headers=h(), json=data)
    again = client.post("/v1/workflows", headers=h(), json=data)
    assert first.json()["id"] == again.json()["id"]
    assert client.post("/v1/workflows", headers=h(), json={**data, "steps": [step("b")]}).status_code == 409
    canceled = client.post("/v1/workflows/" + first.json()["id"] + "/cancel", headers=h())
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["state"] == "canceled"
    assert canceled.json()["steps"][0]["state"] in ("completed", "canceled")


def test_reopen_database_persists_workflows(mesh):
    client, app = mesh
    r = submit(client, [step("a")])
    result = await_workflow(client, r.json()["id"])
    reopened = WorkflowEngine(Store(app.state.store.path))
    assert reopened.get(r.json()["id"])["state"] == "completed"
    assert result["steps"][0]["output"] == "ok"


def test_invalid_token_project_roles(monkeypatch):
    monkeypatch.setenv("MESH_TOKENS", json.dumps({"valid-token-123456": {
        "name": "alice", "project_roles": {"p": "owner"}}}))
    with pytest.raises(ValueError, match="project_roles"):
        load_tokens()
