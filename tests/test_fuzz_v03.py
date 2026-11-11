"""Deterministic generative tests without an optional property-testing dependency."""
import json
import random
from pathlib import Path

from fastapi.testclient import TestClient

from agentmesh.app import create_app
from agentmesh.workflows import WorkflowStep, validate_steps, _resolve


def test_generated_valid_dag_and_nested_references():
    rng = random.Random(103)
    for length in range(1, 25):
        nodes = []
        for i in range(length):
            deps = [f"n{k}" for k in range(i) if rng.random() < .22]
            value = {"step": i, "values": [{"$from": dep} for dep in deps]}
            nodes.append(WorkflowStep(id=f"n{i}", to="echo", input=value,
                                      depends_on=deps, max_attempts=1))
        validate_steps(nodes, None)
        outputs = {f"n{i}": {"answer": i} for i in range(length)}
        for node in nodes:
            resolved = _resolve(node.input, outputs)
            assert resolved["values"] == [outputs[dep] for dep in node.depends_on]


def test_generated_cycles_and_bad_references_rejected():
    rng = random.Random(104)
    for length in range(2, 24):
        nodes = [WorkflowStep(id=f"n{i}", to="echo", input=1,
                    depends_on=[f"n{i-1}"] if i else [f"n{length-1}"])
                 for i in range(length)]
        try:
            validate_steps(nodes, None)
            raise AssertionError(f"cycle was accepted ({length})")
        except ValueError:
            pass
        if rng.random() < .5:
            nodes[-1].depends_on = []
            validate_steps(nodes, None)


def test_fuzz_protocol_jsonrpc_boundaries(tmp_path):
    app = create_app(config_path=str(Path(__file__).parents[1] / "agents.example.json"),
                     db_path=str(tmp_path / "fuzz.db"),
                     tokens={"safe-token-123456": {"name": "fuzz", "allowed_agents": ["echo"]}})
    headers = {"Authorization": "Bearer safe-token-123456", "A2A-Version": "1.0"}
    mcp_headers = {"Authorization": "Bearer safe-token-123456", "MCP-Protocol-Version": "2026-07-28",
                   "Mcp-Method": "tools/call", "Mcp-Name": "mesh_submit_task"}
    invalids = [None, [], {}, True, 2, "", "a" * 400, {"$ref": "x"},
                {"to": "echo"}, {"to": 4, "input": "x"},
                {"to": "not-there", "input": 3}, {"to": "echo", "input": 1, "surprise": "x"}]
    with TestClient(app) as client:
        for value in invalids:
            a2a = client.post("/a2a/echo", headers=headers,
                json={"jsonrpc": "2.0", "id": 77, "method": "SendMessage", "params": value})
            assert a2a.status_code < 500, (value, a2a.text)
            mcp = client.post("/mcp", headers=mcp_headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "mesh_submit_task", "arguments": value}})
            assert mcp.status_code < 500, (value, mcp.text)
            assert "Traceback" not in mcp.text
