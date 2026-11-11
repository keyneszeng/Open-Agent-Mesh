"""Zero-extra-dependency Python caller for arbitrary Python agents/tools."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.error
import urllib.request


class MeshClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(self, path: str, method="GET", data=None):
        content = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=content,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def agents(self):
        return self._call("/v1/agents")

    def facts(self, project_id):
        return self._call("/v1/projects/" + urllib.parse.quote(project_id, safe="") + "/facts")

    def fact(self, project_id, key):
        return self._call("/v1/projects/" + urllib.parse.quote(project_id, safe="") + "/facts/" + urllib.parse.quote(key, safe=""))

    def put_fact(self, project_id, key, value, *, expected_revision):
        return self._call("/v1/projects/" + urllib.parse.quote(project_id, safe="") + "/facts/" + urllib.parse.quote(key, safe=""), "PUT",
                          {"value": value, "expected_revision": expected_revision})

    def submit(self, to, value, *, context=None, idempotency_key=None, parent_task_id=None):
        return self._call("/v1/tasks", "POST", {"to": to, "input": value,
            "context": context or {}, "idempotency_key": idempotency_key,
            "parent_task_id": parent_task_id})

    def submit_workflow(self, steps, *, project_id=None, idempotency_key=None):
        return self._call("/v1/workflows", "POST", {"steps": steps,
            "project_id": project_id, "idempotency_key": idempotency_key})

    def workflow(self, workflow_id):
        return self._call("/v1/workflows/" + urllib.parse.quote(workflow_id, safe=""))

    def wait_workflow(self, workflow_id, *, seconds=120, interval=0.25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            flow = self.workflow(workflow_id)
            if flow["state"] in ("completed", "failed", "canceled"):
                return flow
            time.sleep(interval)
        raise TimeoutError("Workflow still running; retrieve it again by workflow ID")

    def get(self, task_id):
        return self._call("/v1/tasks/" + task_id)

    def wait(self, task_id, *, seconds=60, interval=0.25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            task = self.get(task_id)
            if task["state"] in ("completed", "failed", "canceled"):
                return task
            time.sleep(interval)
        raise TimeoutError("Task still running; retrieve it again by task ID")

    def run(self, to, value, *, seconds=60, **kwargs):
        task = self.submit(to, value, **kwargs)
        result = self.wait(task["id"], seconds=seconds)
        if result["state"] != "completed":
            raise RuntimeError("Task did not complete: " + str(result["error"]))
        return result["output"]
