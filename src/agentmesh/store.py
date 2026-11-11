"""SQLite durable task journal (single-replica MVP). No task payload in audit events."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL = {"completed", "failed", "canceled"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                request_key TEXT,
                request_hash TEXT NOT NULL,
                input_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                parent_id TEXT,
                depth INTEGER NOT NULL,
                state TEXT NOT NULL,
                output_json TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner, request_key)
              );
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                state TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
              );
              CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, created_at);
              CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
              CREATE TABLE IF NOT EXISTS project_facts (
                project_id TEXT NOT NULL,
                key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                value_json TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, key)
              );
              CREATE TABLE IF NOT EXISTS project_fact_history (
                project_id TEXT NOT NULL,
                key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                value_json TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, key, revision)
              );
            """)
            # Tasks interrupted by a crash return to the queue. Remote side-effects
            # may have occurred; adapters must use idempotency to avoid duplication.
            db.execute("UPDATE tasks SET state='submitted', updated_at=? WHERE state='working'", (now(),))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def get(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def create(self, *, agent_id: str, owner: str, input_value: Any,
               context: dict, request_key: str | None, parent_id: str | None,
               max_depth: int) -> tuple[dict, bool]:
        payload = canonical({"agent": agent_id, "input": input_value, "context": context, "parent": parent_id})
        h = hashlib.sha256(payload.encode()).hexdigest()
        timestamp = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                previous = db.execute("SELECT * FROM tasks WHERE owner=? AND request_key=?", (owner, request_key)).fetchone()
                if previous:
                    if previous["request_hash"] != h:
                        raise ValueError("Idempotency key reused with a different payload")
                    return dict(previous), False
            depth = 0
            if parent_id:
                parent = db.execute("SELECT * FROM tasks WHERE id=?", (parent_id,)).fetchone()
                if not parent or parent["owner"] != owner:
                    raise PermissionError("Parent task not found or not owned by caller")
                depth = parent["depth"] + 1
            if depth > max_depth:
                raise ValueError("Maximum agent delegation depth reached")
            task_id = str(uuid.uuid4())
            db.execute("""
                INSERT INTO tasks (id,agent_id,owner,request_key,request_hash,input_json,context_json,
                                   parent_id,depth,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (task_id, agent_id, owner, request_key, h, canonical(input_value),
                    canonical(context), parent_id, depth, "submitted", timestamp, timestamp))
            self._event(db, task_id, "submitted", "Task accepted")
            return dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()), True

    def _event(self, db, task_id, state, detail):
        db.execute("INSERT INTO events(task_id,state,detail,created_at) VALUES(?,?,?,?)",
                   (task_id, state, detail[:300], now()))

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE state='submitted' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET state='working', attempts=attempts+1, updated_at=? WHERE id=? AND state='submitted'", (now(), row["id"]))
            self._event(db, row["id"], "working", "Worker claimed task")
            return dict(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())

    def finish(self, task_id: str, state: str, output: Any = None, error: str | None = None) -> bool:
        if state not in ("completed", "failed"):
            raise ValueError("invalid finish state")
        with self.connect() as db:
            cur = db.execute("""
                UPDATE tasks SET state=?, output_json=?, error=?, updated_at=?
                WHERE id=? AND state='working'
            """, (state, canonical(output) if state == "completed" else None,
                   error[:400] if error else None, now(), task_id))
            if cur.rowcount:
                self._event(db, task_id, state, "Task finished" if state == "completed" else "Task execution failed")
            return bool(cur.rowcount)

    def cancel(self, task_id: str) -> dict:
        with self.connect() as db:
            cur = db.execute("UPDATE tasks SET state='canceled', updated_at=? WHERE id=? AND state IN ('submitted','working')", (now(), task_id))
            if cur.rowcount:
                self._event(db, task_id, "canceled", "Cancellation requested; underlying remote work may continue")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row)

    def events(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT seq,state,detail,created_at FROM events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        return [dict(x) for x in rows]

    def list(self, owner: str | None = None, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            if owner is None:
                rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM tasks WHERE owner=? ORDER BY created_at DESC LIMIT ?", (owner, limit)).fetchall()
        return [dict(x) for x in rows]


    def get_fact(self, project_id: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM project_facts WHERE project_id=? AND key=?", (project_id, key)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["value"] = json.loads(result.pop("value_json"))
        return result

    def list_facts(self, project_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM project_facts WHERE project_id=? ORDER BY key LIMIT 1000", (project_id,)).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["value"] = json.loads(result.pop("value_json"))
            results.append(result)
        return results

    def put_fact(self, project_id: str, key: str, value: Any,
                 author: str, expected_revision: int) -> dict:
        encoded = canonical(value)
        if len(encoded.encode("utf-8")) > 32_000:
            raise ValueError("Fact value exceeds 32 KB")
        timestamp = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT revision FROM project_facts WHERE project_id=? AND key=?", (project_id, key)).fetchone()
            current = existing["revision"] if existing else 0
            if current != expected_revision:
                raise ValueError(f"Revision conflict: current revision is {current}")
            revision = current + 1
            db.execute("""INSERT INTO project_facts (project_id,key,revision,value_json,updated_by,updated_at)
                           VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,key) DO UPDATE SET
                           revision=excluded.revision,value_json=excluded.value_json,
                           updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                       (project_id, key, revision, encoded, author, timestamp))
            db.execute("INSERT INTO project_fact_history (project_id,key,revision,value_json,updated_by,updated_at) VALUES (?,?,?,?,?,?)",
                       (project_id, key, revision, encoded, author, timestamp))
        return self.get_fact(project_id, key)

    def fact_history(self, project_id: str, key: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM project_fact_history WHERE project_id=? AND key=? ORDER BY revision", (project_id, key)).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["value"] = json.loads(result.pop("value_json"))
            results.append(result)
        return results


def public_task(row: dict) -> dict:
    return {
        "id": row["id"], "to": row["agent_id"], "owner": row["owner"],
        "state": row["state"], "parent_task_id": row["parent_id"],
        "depth": row["depth"], "attempts": row["attempts"],
        "output": json.loads(row["output_json"]) if row["output_json"] is not None else None,
        "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
