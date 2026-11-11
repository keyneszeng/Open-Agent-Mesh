"""Persisted, bounded DAG coordination for one gateway process.

No LLM decides what to execute: the caller explicitly approves each registered
agent, dependency and input. All task dispatch still passes Store idempotency.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .store import Store, canonical, now

_IDENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FACT_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_DONE = {"completed", "failed", "skipped", "canceled"}


class WorkflowStep(BaseModel):
    id: str
    to: str
    input: Any
    depends_on: list[str] = Field(default_factory=list)
    fact_keys: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=1, ge=1, le=3)
    approval_required: bool = False


class WorkflowCreate(BaseModel):
    steps: list[WorkflowStep] = Field(min_length=1, max_length=24)
    project_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


def _references(value: Any, depth: int = 0) -> set[str]:
    if depth > 20:
        raise ValueError("Input nesting exceeds 20 levels")
    if isinstance(value, dict):
        if "$from" in value:
            if set(value) != {"$from"} or not isinstance(value["$from"], str):
                raise ValueError("Dependency reference must be exactly {'$from': 'step-id'}")
            return {value["$from"]}
        result: set[str] = set()
        for item in value.values():
            result.update(_references(item, depth + 1))
        return result
    if isinstance(value, list):
        result = set()
        for item in value:
            result.update(_references(item, depth + 1))
        return result
    return set()


def _resolve(value: Any, outputs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$from"}:
            return outputs[value["$from"]]
        return {key: _resolve(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, outputs) for item in value]
    return value


def validate_steps(steps: list[WorkflowStep], project_id: str | None) -> None:
    ids = [step.id for step in steps]
    if len(set(ids)) != len(ids):
        raise ValueError("Workflow step IDs must be unique")
    for step in steps:
        if not _IDENT.fullmatch(step.id):
            raise ValueError("Invalid workflow step ID")
        if len(step.depends_on) != len(set(step.depends_on)):
            raise ValueError("Duplicate dependencies")
        if step.id in step.depends_on or set(step.depends_on) - set(ids):
            raise ValueError("Missing or self-referencing dependency")
        if _references(step.input) - set(step.depends_on):
            raise ValueError("$from references require an explicit depends_on entry")
        if step.fact_keys and not project_id:
            raise ValueError("fact_keys require project_id")
        if len(step.fact_keys) > 20 or len(step.fact_keys) != len(set(step.fact_keys)):
            raise ValueError("Too many or duplicate fact keys")
        if any(not _FACT_KEY.fullmatch(key) for key in step.fact_keys):
            raise ValueError("Invalid fact key")
    graph = {step.id: step.depends_on for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str):
        if node in visiting:
            raise ValueError("Workflow dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for step_id in ids:
        visit(step_id)


class WorkflowEngine:
    def __init__(self, tasks: Store):
        self.tasks = tasks
        with tasks.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, project_id TEXT,
                  request_key TEXT, request_hash TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(owner, request_key)
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                  workflow_id TEXT NOT NULL, step_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL, input_json TEXT NOT NULL,
                  dependencies_json TEXT NOT NULL, facts_json TEXT NOT NULL,
                  max_attempts INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  task_id TEXT, state TEXT NOT NULL DEFAULT 'pending',
                  available_at TEXT, error TEXT,
                  PRIMARY KEY(workflow_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  workflow_id TEXT NOT NULL, step_id TEXT,
                  kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_state ON workflows(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_workflow_events ON workflow_events(workflow_id, seq);
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(workflow_steps)")}
            for column, definition in {
                "approval_required": "INTEGER NOT NULL DEFAULT 0",
                "approval_status": "TEXT",
                "reviewed_by": "TEXT",
                "reviewed_at": "TEXT",
            }.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE workflow_steps ADD COLUMN {column} {definition}")

    @staticmethod
    def _event(db: sqlite3.Connection, workflow_id: str, step_id: str | None,
               kind: str, detail: str):
        db.execute("INSERT INTO workflow_events(workflow_id,step_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                   (workflow_id, step_id, kind, detail, now()))

    def create(self, owner: str, data: WorkflowCreate) -> dict:
        validate_steps(data.steps, data.project_id)
        spec = {"project_id": data.project_id, "steps": [s.model_dump(mode="json") for s in data.steps]}
        encoded = canonical(spec)
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("Workflow definition exceeds 256 KB")
        request_hash = hashlib.sha256(encoded.encode()).hexdigest()
        timestamp = now()
        workflow_id = str(uuid.uuid4())
        with self.tasks.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if data.idempotency_key:
                old = db.execute("SELECT id,request_hash FROM workflows WHERE owner=? AND request_key=?",
                                 (owner, data.idempotency_key)).fetchone()
                if old:
                    if old["request_hash"] != request_hash:
                        raise ValueError("Idempotency key reused with a different workflow")
                    return self.get(old["id"])
            facts: dict[str, Any] = {}
            for step in data.steps:
                for key in step.fact_keys:
                    if key not in facts:
                        fact = self.tasks.get_fact(data.project_id, key)
                        if fact is None:
                            raise LookupError("Selected project fact not found")
                        facts[key] = {"value": fact["value"], "revision": fact["revision"]}
            db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?)",
                       (workflow_id, owner, data.project_id, data.idempotency_key,
                        request_hash, "running", timestamp, timestamp))
            for step in data.steps:
                selected = {key: facts[key] for key in step.fact_keys}
                db.execute("""INSERT INTO workflow_steps
                   (workflow_id,step_id,agent_id,input_json,dependencies_json,facts_json,max_attempts,approval_required,state)
                   VALUES(?,?,?,?,?,?,?,?,'pending')""",
                   (workflow_id, step.id, step.to, canonical(step.input),
                    canonical(step.depends_on), canonical(selected), step.max_attempts,
                    int(step.approval_required)))
            self._event(db, workflow_id, None, "created", "Workflow accepted")
        return self.get(workflow_id)

    def _raw(self, workflow_id: str) -> tuple[dict | None, list[dict]]:
        with self.tasks.connect() as db:
            workflow = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            nodes = db.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY rowid", (workflow_id,)).fetchall()
        return (dict(workflow) if workflow else None, [dict(n) for n in nodes])

    def get(self, workflow_id: str) -> dict | None:
        workflow, nodes = self._raw(workflow_id)
        if not workflow:
            return None
        return {
            "id": workflow["id"], "owner": workflow["owner"],
            "project_id": workflow["project_id"], "state": workflow["state"],
            "created_at": workflow["created_at"], "updated_at": workflow["updated_at"],
            "steps": [self._public_step(node) for node in nodes],
        }

    def _public_step(self, node: dict) -> dict:
        task = self.tasks.get(node["task_id"]) if node["task_id"] else None
        return {
            "id": node["step_id"], "to": node["agent_id"], "state": node["state"],
            "depends_on": json.loads(node["dependencies_json"]),
            "attempts": node["attempts"], "max_attempts": node["max_attempts"],
            "task_id": node["task_id"],
            "output": json.loads(task["output_json"]) if task and node["state"] == "completed" else None,
            "error": node["error"],
            "approval_required": bool(node["approval_required"]),
            "approval_status": node["approval_status"],
            "reviewed_by": node["reviewed_by"],
            "reviewed_at": node["reviewed_at"],
        }

    def list(self, owner: str | None) -> list[dict]:
        with self.tasks.connect() as db:
            if owner is None:
                ids = db.execute("SELECT id FROM workflows ORDER BY created_at DESC LIMIT 50").fetchall()
            else:
                ids = db.execute("SELECT id FROM workflows WHERE owner=? ORDER BY created_at DESC LIMIT 50",
                                 (owner,)).fetchall()
        return [self.get(row["id"]) for row in ids]

    def events(self, workflow_id: str) -> list[dict]:
        with self.tasks.connect() as db:
            rows = db.execute("SELECT seq,step_id,kind,detail,created_at FROM workflow_events WHERE workflow_id=? ORDER BY seq",
                              (workflow_id,)).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, workflow_id: str) -> dict:
        to_cancel: list[str] = []
        with self.tasks.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            workflow = db.execute("SELECT state FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if workflow and workflow["state"] == "running":
                nodes = db.execute("SELECT step_id,state,task_id FROM workflow_steps WHERE workflow_id=?",
                                   (workflow_id,)).fetchall()
                for node in nodes:
                    if node["state"] not in _DONE:
                        if node["task_id"]:
                            to_cancel.append(node["task_id"])
                        db.execute("UPDATE workflow_steps SET state='canceled',error=NULL WHERE workflow_id=? AND step_id=?",
                                   (workflow_id, node["step_id"]))
                        self._event(db, workflow_id, node["step_id"], "canceled", "Step canceled")
                db.execute("UPDATE workflows SET state='canceled',updated_at=? WHERE id=?", (now(), workflow_id))
                self._event(db, workflow_id, None, "canceled", "Workflow canceled")
        # Release workflow's SQLite write lock before canceling underlying tasks.
        for task_id in to_cancel:
            self.tasks.cancel(task_id)
        return self.get(workflow_id)

    def review(self, workflow_id: str, step_id: str, *, actor: str, approve: bool) -> dict:
        """Transactional human gate: cannot be replayed or approved after cancel."""
        with self.tasks.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            workflow = db.execute("SELECT state FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if not workflow or workflow["state"] != "running":
                raise ValueError("Workflow is not running")
            row = db.execute("SELECT state,approval_required FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                             (workflow_id, step_id)).fetchone()
            if not row or not row["approval_required"] or row["state"] != "awaiting_approval":
                raise ValueError("Step is not awaiting approval")
            next_state = "pending" if approve else "failed"
            db.execute("""UPDATE workflow_steps SET state=?,approval_status=?,reviewed_by=?,reviewed_at=?,error=?
                         WHERE workflow_id=? AND step_id=? AND state='awaiting_approval'""",
                       (next_state, "approved" if approve else "rejected", actor, now(),
                        None if approve else "Human reviewer rejected step", workflow_id, step_id))
            self._event(db, workflow_id, step_id, "approved" if approve else "rejected",
                        f"Human review by {actor}")
            db.execute("UPDATE workflows SET updated_at=? WHERE id=?", (now(), workflow_id))
        return self.get(workflow_id)

    def _set_step(self, workflow_id: str, step_id: str, *, state: str,
                  task_id: str | None = None, attempts: int | None = None,
                  available_at: str | None = None, error: str | None = None):
        with self.tasks.connect() as db:
            if attempts is None:
                db.execute("""UPDATE workflow_steps SET state=?, task_id=?, available_at=?, error=?
                              WHERE workflow_id=? AND step_id=?""",
                           (state, task_id, available_at, error, workflow_id, step_id))
            else:
                db.execute("""UPDATE workflow_steps SET state=?, task_id=?, attempts=?, available_at=?, error=?
                              WHERE workflow_id=? AND step_id=?""",
                           (state, task_id, attempts, available_at, error, workflow_id, step_id))
            self._event(db, workflow_id, step_id, state, f"Step state is {state}")
            db.execute("UPDATE workflows SET updated_at=? WHERE id=?", (now(), workflow_id))

    def tick(self):
        """Idempotent scheduler pass. Call from a single-process background worker."""
        with self.tasks.connect() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM workflows WHERE state='running' ORDER BY created_at LIMIT 50")]
        for workflow_id in ids:
            self._advance(workflow_id)

    def _advance(self, workflow_id: str):
        workflow, rows = self._raw(workflow_id)
        if not workflow or workflow["state"] != "running":
            return
        for node in rows:
            if node["state"] != "running":
                continue
            task = self.tasks.get(node["task_id"])
            if task and task["state"] == "completed":
                self._set_step(workflow_id, node["step_id"], state="completed", task_id=node["task_id"])
            elif task and task["state"] == "failed":
                if node["attempts"] < node["max_attempts"]:
                    delay = min(30, 2 ** (node["attempts"] - 1))
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
                    self._set_step(workflow_id, node["step_id"], state="pending", available_at=retry_at)
                else:
                    self._set_step(workflow_id, node["step_id"], state="failed", task_id=node["task_id"],
                                   error="Agent execution failed after allowed attempts")
            elif task and task["state"] == "canceled":
                self._set_step(workflow_id, node["step_id"], state="canceled", task_id=node["task_id"])
        _, rows = self._raw(workflow_id)
        by_id = {node["step_id"]: node for node in rows}
        for node in rows:
            if node["state"] != "pending":
                continue
            deps = json.loads(node["dependencies_json"])
            if any(by_id[dep]["state"] in ("failed", "canceled", "skipped") for dep in deps):
                self._set_step(workflow_id, node["step_id"], state="skipped", error="Dependency did not complete")
                by_id[node["step_id"]]["state"] = "skipped"
                continue
            if any(by_id[dep]["state"] != "completed" for dep in deps):
                continue
            if node["available_at"] and node["available_at"] > now():
                continue
            if node["approval_required"] and node["approval_status"] != "approved":
                self._set_step(workflow_id, node["step_id"], state="awaiting_approval")
                by_id[node["step_id"]]["state"] = "awaiting_approval"
                continue
            try:
                outputs = {dep: json.loads(self.tasks.get(by_id[dep]["task_id"])["output_json"]) for dep in deps}
                value = _resolve(json.loads(node["input_json"]), outputs)
                facts = json.loads(node["facts_json"])
                context = {"mesh_workflow": {"id": workflow_id, "step_id": node["step_id"],
                                             "project_id": workflow["project_id"], "facts": facts}}
                if len(canonical({"input": value, "context": context}).encode()) > 128_000:
                    raise ValueError("Resolved step exceeds task payload limit")
                attempt = node["attempts"] + 1
                task, _ = self.tasks.create(agent_id=node["agent_id"], owner=workflow["owner"],
                    input_value=value, context=context,
                    request_key=f"workflow:{workflow_id}:{node['step_id']}:{attempt}",
                    parent_id=None, max_depth=0)
                self._set_step(workflow_id, node["step_id"], state="running",
                               task_id=task["id"], attempts=attempt)
                by_id[node["step_id"]]["state"] = "running"
            except Exception:
                # Details may contain private agent output; avoid exposing them via API/audit.
                self._set_step(workflow_id, node["step_id"], state="failed",
                               error="Workflow step could not be dispatched")
                by_id[node["step_id"]]["state"] = "failed"
        _, rows = self._raw(workflow_id)
        states = {node["state"] for node in rows}
        if states <= _DONE:
            outcome = "completed" if states == {"completed"} else "failed"
            with self.tasks.connect() as db:
                db.execute("UPDATE workflows SET state=?, updated_at=? WHERE id=? AND state='running'",
                           (outcome, now(), workflow_id))
                self._event(db, workflow_id, None, outcome, f"Workflow {outcome}")


def install_workflow_api(app, engine: WorkflowEngine, identity, registry, can_use, check_project):
    def owned(workflow_id: str, profile: dict) -> dict:
        row = engine.get(workflow_id)
        if not row or (profile.get("role") != "admin" and row["owner"] != profile["name"]):
            raise HTTPException(404, "Workflow not found")
        return row

    @app.post("/v1/workflows", status_code=202)
    async def create_workflow(data: WorkflowCreate, profile: dict = Depends(identity)):
        if data.project_id is not None:
            check_project(data.project_id, profile)
        for step in data.steps:
            if step.to not in registry:
                raise HTTPException(404, "Agent not found")
            if not can_use(profile, step.to):
                raise HTTPException(403, "Agent not allowed")
        # Approval is a server-side agent policy, never a flag the caller may disable.
        effective = [step.model_copy(update={"approval_required": True}) if registry[step.to].requires_approval
                     else step for step in data.steps]
        data = data.model_copy(update={"steps": effective})
        try:
            return engine.create(profile["name"], data)
        except LookupError:
            raise HTTPException(404, "Project fact not found") from None
        except ValueError as exc:
            status = 409 if "Idempotency key" in str(exc) else 422
            raise HTTPException(status, str(exc)) from None

    @app.get("/v1/workflows")
    async def list_workflows(profile: dict = Depends(identity)):
        return engine.list(None if profile.get("role") == "admin" else profile["name"])

    @app.get("/v1/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str, profile: dict = Depends(identity)):
        return owned(workflow_id, profile)

    @app.get("/v1/workflows/{workflow_id}/events")
    async def workflow_events(workflow_id: str, profile: dict = Depends(identity)):
        owned(workflow_id, profile)
        return engine.events(workflow_id)

    @app.post("/v1/workflows/{workflow_id}/cancel")
    async def cancel_workflow(workflow_id: str, profile: dict = Depends(identity)):
        owned(workflow_id, profile)
        return engine.cancel(workflow_id)

    def review_access(workflow_id: str, profile: dict):
        workflow = engine.get(workflow_id)
        if workflow is None:
            raise HTTPException(404, "Workflow not found")
        if profile["name"] == workflow["owner"]:
            raise HTTPException(403, "A different human principal must review the step")
        if profile.get("role") != "admin" and not profile.get("can_approve", False):
            raise HTTPException(403, "Human approval permission required")
        if workflow["project_id"]:
            check_project(workflow["project_id"], profile, write=True)
        return workflow

    @app.post("/v1/workflows/{workflow_id}/steps/{step_id}/approve")
    async def approve_step(workflow_id: str, step_id: str, profile: dict = Depends(identity)):
        review_access(workflow_id, profile)
        try:
            return engine.review(workflow_id, step_id, actor=profile["name"], approve=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/v1/workflows/{workflow_id}/steps/{step_id}/reject")
    async def reject_step(workflow_id: str, step_id: str, profile: dict = Depends(identity)):
        review_access(workflow_id, profile)
        try:
            return engine.review(workflow_id, step_id, actor=profile["name"], approve=False)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
