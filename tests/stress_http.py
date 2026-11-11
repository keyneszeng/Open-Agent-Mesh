"""Deterministic loopback load smoke: concurrent callers, idempotency and DAGs.

No external load tool required; checks correctness, not a performance SLA.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 19852
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "local-stress-token-123456789"


def call(path, method="GET", payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method,
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.load(response)


with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), MESH_PORT=str(PORT),
               MESH_DB_PATH=str(td / "mesh.db"), MESH_AGENTS_CONFIG=str(ROOT / "agents.example.json"),
               MESH_TOKENS=json.dumps({TOKEN: {"name": "load", "role": "admin", "allowed_agents": ["*"]}}))
    proc = subprocess.Popen([sys.executable, "-m", "agentmesh.cli"], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                call("/health")
                break
            except Exception:
                time.sleep(.07)
        else:
            raise RuntimeError("service failed to start")
        start = time.monotonic()

        def submit(i):
            task = {"to": "echo", "input": {"n": i, "tag": "load"}, "idempotency_key": f"load-{i}"}
            a = call("/v1/tasks", "POST", task)
            b = call("/v1/tasks", "POST", task)
            assert a["id"] == b["id"]
            return a["id"]

        with ThreadPoolExecutor(max_workers=12) as workers:
            task_ids = list(workers.map(submit, range(100)))
        assert len(set(task_ids)) == 100
        workflows = []
        for i in range(10):
            steps = [{"id": "a", "to": "uppercase", "input": f"hello{i}"},
                     {"id": "b", "to": "echo", "input": {"from": {"$from": "a"}}, "depends_on": ["a"]},
                     {"id": "c", "to": "echo", "input": i}]
            workflows.append(call("/v1/workflows", "POST", {"steps": steps, "idempotency_key": f"flow-{i}"})["id"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            tasks = [call("/v1/tasks/" + key) for key in task_ids]
            flows = [call("/v1/workflows/" + key) for key in workflows]
            if all(x["state"] == "completed" for x in tasks + flows):
                break
            time.sleep(.10)
        else:
            raise AssertionError("load did not settle within deadline")
        assert sorted(x["output"]["n"] for x in tasks) == list(range(100))
        assert all(x["steps"][1]["output"]["from"] == f"HELLO{i}" for i, x in enumerate(flows))
        duration = round(time.monotonic() - start, 2)
        print(json.dumps({"requests_submitted": 200, "unique_tasks": 100,
                          "workflows": 10, "workflow_steps": 30,
                          "concurrent_submitters": 12, "elapsed_seconds": duration, "result": "PASS"}))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=3)
