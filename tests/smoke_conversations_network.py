"""Real process integration: Mesh -> A2A gateway + external HTTP Agent in one conversation."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import closing

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "local-conversation-network-token-0123456"
HTTP_TOKEN = "http-agent-demo-authentication-token"
REMOTE, FRONT, WEBHOOK = 19901, 19902, 19903
processes = []


def start(cmd, **extra):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
               MESH_TOKENS=json.dumps({TOKEN: {"name": "network-test", "role": "admin", "allowed_agents": ["*"]}}),
               REMOTE_BEARER_TOKEN=TOKEN, MESH_HTTP_AGENT_TOKEN=HTTP_TOKEN, **extra)
    processes.append(subprocess.Popen(cmd, env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def ready(port):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=1):
                return
        except Exception:
            time.sleep(.075)
    raise RuntimeError(f"Server {port} not ready")


try:
    with tempfile.TemporaryDirectory() as temp:
        tmp = pathlib.Path(temp)
        remote_db = str(tmp / "remote.sqlite")
        front_config = tmp / "front.json"
        front_config.write_text(json.dumps({"agents": [
            {"id": "remote-upper", "kind": "a2a_jsonrpc", "name": "Remote A2A", "skills": ["text"],
             "url": f"http://127.0.0.1:{REMOTE}/a2a/uppercase", "token_env": "REMOTE_BEARER_TOKEN"},
            {"id": "external-http", "kind": "http_json", "name": "Webhook", "skills": ["webhook"],
             "url": f"http://127.0.0.1:{WEBHOOK}/invoke", "token_env": "MESH_HTTP_AGENT_TOKEN"},
        ]}))
        start([sys.executable, "-m", "agentmesh.cli"], MESH_PORT=str(REMOTE),
              MESH_DB_PATH=remote_db, MESH_AGENTS_CONFIG=str(ROOT / "agents.example.json"))
        start([sys.executable, "-m", "agentmesh.cli"], MESH_PORT=str(FRONT),
              MESH_DB_PATH=str(tmp / "front.sqlite"), MESH_AGENTS_CONFIG=str(front_config))
        start([sys.executable, "-m", "uvicorn", "examples.http_agent:app", "--host", "127.0.0.1", "--port", str(WEBHOOK)])
        for port in (REMOTE, FRONT, WEBHOOK):
            ready(port)
        from agentmesh.client import MeshClient
        caller = MeshClient(f"http://127.0.0.1:{FRONT}", TOKEN)
        cid = caller.create_conversation("Cross-platform discussion", ["remote-upper", "external-http"], max_turns=4)["id"]
        caller.start_discussion(cid, "test cross agent discussion", rounds=4, idempotency_key="real-network-test")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            conv = caller.conversation(cid)
            if len(conv["turns"]) == 4 and all(t["state"] == "completed" for t in conv["turns"]):
                break
            time.sleep(.10)
        else:
            raise AssertionError("Real A2A+HTTP conversation did not finish")
        assert [t["agent_id"] for t in conv["turns"]] == ["remote-upper", "external-http"] * 2
        messages = caller.conversation_messages(cid)
        assert len(messages) == 5, messages
        assert messages[1]["content"] == "TEST CROSS AGENT DISCUSSION"
        assert messages[2]["content"].startswith("processed by external agent:")
        assert messages[3]["content"].startswith("PROCESSED BY EXTERNAL AGENT:")
        assert len({m["seq"] for m in messages}) == 5
        with closing(sqlite3.connect(remote_db)) as db:
            contexts = [json.loads(r[0]).get("_a2a_context_id") for r in
                        db.execute("SELECT context_json FROM tasks ORDER BY created_at")]
        assert contexts == [cid, cid], contexts
        print(json.dumps({"result": "PASS", "conversation_id": cid, "turns": len(conv["turns"]),
                          "messages": len(messages), "gateway_processes": 2,
                          "remote_a2a_context_propagation": "PASS", "http_adapter": "PASS"}))
finally:
    for proc in processes:
        proc.terminate()
    for proc in processes:
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
