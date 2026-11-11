"""Real-process failure injection: SIGKILL, restart, lease recovery and A2A polling.

Only binds localhost. No external service or credential is used.
"""
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 19851
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "chaos-local-test-token-do-not-reuse"
procs = []


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def wait_state(task_id, expected, *, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            item = call("/v1/tasks/" + task_id)
            if item["state"] in expected:
                return item
        except (ConnectionError, urllib.error.URLError):
            pass
        time.sleep(.08)
    raise AssertionError(f"task {task_id} did not reach {expected}")


def start(config, db):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
               MESH_TOKENS=json.dumps({TOKEN: {"name": "chaos", "role": "admin", "allowed_agents": ["*"]}}),
               MESH_LEASE_SECONDS="2", MESH_DB_PATH=str(db), MESH_PORT=str(PORT),
               MESH_AGENTS_CONFIG=str(config))
    proc = subprocess.Popen([sys.executable, "-m", "agentmesh.cli"], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(proc)
    for _ in range(90):
        try:
            call("/health")
            return proc
        except (ConnectionError, urllib.error.URLError):
            time.sleep(.08)
    raise RuntimeError("gateway failed to start")


try:
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        db = td / "mesh.db"
        config = td / "agents.json"
        config.write_text(json.dumps({"agents": [{"id": "slow", "kind": "python",
            "handler": "examples.slow_agent:slow_echo", "timeout_seconds": 12}]}))
        proc = start(config, db)
        created = call("/v1/tasks", "POST", {"to": "slow", "input": "survived", "idempotency_key": "chaos-1"})
        task_id = created["id"]
        running = wait_state(task_id, {"working"}, seconds=6)
        assert running["attempts"] == 1
        proc.kill()
        proc.wait(timeout=4)
        proc2 = start(config, db)
        # Restart must not steal a *valid* active lease before its expiry.
        resumed = wait_state(task_id, {"completed"}, seconds=14)
        assert resumed["output"] == "survived"
        assert resumed["attempts"] == 2, resumed
        events = call("/v1/tasks/" + task_id + "/events")
        assert [e["state"] for e in events] == ["submitted", "working", "submitted", "working", "completed"]
        assert call("/v1/tasks", "POST", {"to": "slow", "input": "survived", "idempotency_key": "chaos-1"})["id"] == task_id
        print(json.dumps({"scenario": "worker SIGKILL + restart + expired lease recovery",
                          "task_id": task_id, "attempts": resumed["attempts"],
                          "events": [e["state"] for e in events], "result": "PASS"}))
finally:
    for p in procs:
        if p.poll() is None:
            p.terminate()
        try:
            p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait(timeout=3)
