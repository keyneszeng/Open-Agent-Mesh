"""Conversation load smoke: concurrent HTTP creators, independent bounded discussions."""
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
PORT = 19905
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "local-conversation-load-token-123456"
N = 25


def call(path, method="GET", body=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Authorization": "Bearer " + TOKEN,
                                                             "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


with tempfile.TemporaryDirectory() as directory:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), MESH_PORT=str(PORT),
               MESH_DB_PATH=str(pathlib.Path(directory) / "mesh.sqlite"),
               MESH_AGENTS_CONFIG=str(ROOT / "agents.example.json"),
               MESH_TOKENS=json.dumps({TOKEN: {"name": "load", "role": "admin", "allowed_agents": ["*"]}}))
    process = subprocess.Popen([sys.executable, "-m", "agentmesh.cli"], env=env, cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if call("/health")["ok"]:
                    break
            except Exception:
                time.sleep(.08)
        else:
            raise AssertionError("Gateway failed to start")
        start = time.monotonic()
        def create(index):
            cid = call("/v1/conversations", "POST", {
                "title": f"Concurrent #{index}", "participants": ["uppercase", "echo"], "max_turns": 3})["id"]
            data = {"prompt": f"round {index}", "rounds": 3, "idempotency_key": f"discussion-{index}"}
            first = call(f"/v1/conversations/{cid}/discussions", "POST", data)
            replay = call(f"/v1/conversations/{cid}/discussions", "POST", data)
            assert first["message_seq"] == replay["message_seq"] and replay["replayed"]
            return cid
        with ThreadPoolExecutor(max_workers=10) as pool:
            conversations = list(pool.map(create, range(N)))
        assert len(set(conversations)) == N
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            values = [call(f"/v1/conversations/{cid}") for cid in conversations]
            if all(len(c["turns"]) == 3 and all(t["state"] == "completed" for t in c["turns"]) for c in values):
                break
            time.sleep(.13)
        else:
            states = [[x["state"] for x in c["turns"]] for c in values]
            raise AssertionError(f"Conversation load failed: {states}")
        all_messages = [call(f"/v1/conversations/{cid}/messages") for cid in conversations]
        assert all(len(messages) == 4 for messages in all_messages)
        elapsed = round(time.monotonic() - start, 2)
        print(json.dumps({"result": "PASS", "conversations": N, "agent_turns": N * 3,
                          "messages": N * 4, "idempotent_submissions": N,
                          "concurrent_creators": 10, "elapsed_seconds": elapsed}))
    finally:
        process.terminate()
        try: process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
