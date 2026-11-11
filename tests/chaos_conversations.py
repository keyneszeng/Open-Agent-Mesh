"""Crash during live conversation, restart gateway, verify lease recovery and one reply."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 19904
TOKEN = "local-conversation-chaos-token-0123456"
URL = f"http://127.0.0.1:{PORT}"


def call(path, data=None):
    req = urllib.request.Request(URL + path, method="POST" if data is not None else "GET",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=5) as res:
        return json.load(res)


def await_online():
    until = time.monotonic() + 10
    while time.monotonic() < until:
        try:
            if call("/health")["ok"]:
                return
        except Exception:
            time.sleep(.07)
    raise AssertionError("Gateway did not start")


with tempfile.TemporaryDirectory() as folder:
    tmp = pathlib.Path(folder)
    config = tmp / "agents.json"
    config.write_text(json.dumps({"agents": [{"id": "slow", "kind": "python", "handler": "agentmesh.builtins:slow_demo"},
                                               {"id": "echo", "kind": "python", "handler": "agentmesh.builtins:echo"}]}))
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), MESH_PORT=str(PORT),
               MESH_DB_PATH=str(tmp / "mesh.sqlite"), MESH_AGENTS_CONFIG=str(config),
               MESH_LEASE_SECONDS="2", MESH_TOKENS=json.dumps({TOKEN: {
                   "name": "crash", "role": "admin", "allowed_agents": ["*"]}}))
    proc = None
    def run():
        return subprocess.Popen([sys.executable, "-m", "agentmesh.cli"], env=env, cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc = run()
        await_online()
        cid = call("/v1/conversations", {"title": "Crash resilience", "participants": ["slow", "echo"], "max_turns": 2})["id"]
        call(f"/v1/conversations/{cid}/discussions", {"prompt": "recover me", "rounds": 2})
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            conv = call(f"/v1/conversations/{cid}")
            if conv["turns"] and conv["turns"][0]["task_id"]:
                task = call("/v1/tasks/" + conv["turns"][0]["task_id"])
                if task["state"] == "working":
                    break
            time.sleep(.04)
        else:
            raise AssertionError("Task never started")
        proc.kill()
        proc.wait(timeout=3)
        proc = run()
        await_online()
        until = time.monotonic() + 16
        while time.monotonic() < until:
            conv = call(f"/v1/conversations/{cid}")
            if len(conv["turns"]) == 2 and all(t["state"] == "completed" for t in conv["turns"]):
                break
            time.sleep(.16)
        else:
            raise AssertionError("Conversation did not recover after SIGKILL")
        messages = call(f"/v1/conversations/{cid}/messages")
        assert len(messages) == 3 and [m["sender"] for m in messages] == ["user:crash", "agent:slow", "agent:echo"]
        tasks = [call("/v1/tasks/" + t["task_id"]) for t in conv["turns"]]
        assert tasks[0]["attempts"] >= 2, tasks
        assert len({t["task_id"] for t in conv["turns"]}) == 2
        print(json.dumps({"result": "PASS", "sigkill_restart": True,
                          "recovered_attempts": tasks[0]["attempts"],
                          "turns": len(conv["turns"]), "messages": len(messages)}))
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
