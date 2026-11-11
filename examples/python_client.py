"""Run with: MESH_TOKEN='...' PYTHONPATH=src python examples/python_client.py"""
import os
from agentmesh.client import MeshClient

mesh = MeshClient(os.environ.get("MESH_URL", "http://127.0.0.1:8765"), os.environ["MESH_TOKEN"])
print("Available:", [agent["id"] for agent in mesh.agents()])
print("Result:", mesh.run("uppercase", "Any Python agent can delegate tasks"))
