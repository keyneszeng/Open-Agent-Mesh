"""Durable, bounded multi-agent conversations built on the existing task mesh.

Private Mesh REST extension, not a claim of full A2A/MCP chat compatibility.
Messages from agents are untrusted data; they never become executable commands.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio
import time
from pydantic import BaseModel, Field

from .store import Store, canonical, now

_PROJECT = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
MAX_TEXT_BYTES = 4096
MAX_CONTEXT_MESSAGES = 12


class ConversationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    participants: list[str] = Field(min_length=1, max_length=8)
    project_id: str | None = None
    max_turns: int = Field(default=12, ge=1, le=40)


class ConversationMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    to: str
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ConversationDiscussion(BaseModel):
    prompt: str = Field(min_length=1, max_length=4096)
    rounds: int = Field(default=4, ge=2, le=20)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


def _text(value: Any) -> str:
    if isinstance(value, str):
        result = value
    else:
        try:
            result = canonical(value)
        except (ValueError, TypeError):
            result = "[non-serializable result]"
    encoded = result.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        result = encoded[:MAX_TEXT_BYTES - 3].decode("utf-8", errors="ignore") + "..."
    return result


class ConversationEngine:
    def __init__(self, store: Store, *, blocked_agents: set[str] | None = None):
        self.store = store
        self.blocked_agents = frozenset(blocked_agents or ())
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, project_id TEXT,
                    title TEXT NOT NULL, state TEXT NOT NULL, max_turns INTEGER NOT NULL,
                    requested_turns INTEGER NOT NULL DEFAULT 0,
                    auto_remaining INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_participants (
                    conversation_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(conversation_id, agent_id),
                    UNIQUE(conversation_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT, role TEXT NOT NULL,
                    content TEXT NOT NULL, request_key TEXT, request_hash TEXT,
                    task_id TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, seq),
                    UNIQUE(conversation_id, sender, request_key)
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    conversation_id TEXT NOT NULL, number INTEGER NOT NULL,
                    agent_id TEXT NOT NULL, source_seq INTEGER NOT NULL,
                    task_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, number)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_turns_state
                    ON conversation_turns(state);
                CREATE INDEX IF NOT EXISTS idx_conversations_owner
                    ON conversations(owner, updated_at);
                CREATE TABLE IF NOT EXISTS conversation_stream (
                    conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                    task_id TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_conv_stream_task
                    ON conversation_stream(conversation_id,task_id,kind);
                CREATE TABLE IF NOT EXISTS conversation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL, kind TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def _feed(self, db, cid: str, kind: str, payload: dict, task_id: str | None = None):
        # Per-conversation monotonically increasing offsets, safe under SQLite write lock.
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM conversation_stream WHERE conversation_id=?", (cid,)).fetchone()[0]
        db.execute("INSERT INTO conversation_stream VALUES (?,?,?,?,?,?)",
                   (cid, seq, kind, canonical(payload), task_id, now()))
        return seq

    def stream_events(self, cid: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute("""SELECT seq,kind,payload_json,task_id,created_at
                FROM conversation_stream WHERE conversation_id=? AND seq>?
                ORDER BY seq LIMIT ?""", (cid, after, limit)).fetchall()
        return [{"seq": row["seq"], "kind": row["kind"],
                 "data": json.loads(row["payload_json"]), "task_id": row["task_id"],
                 "created_at": row["created_at"]} for row in rows]

    def emit_delta(self, cid: str, task_id: str, lease_token: str, text: str) -> bool:
        """Accept untrusted live text only for the currently fenced execution attempt."""
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 4096:
            raise ValueError("Streaming delta exceeds 4096 bytes or is empty")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT t.attempts FROM tasks t
                JOIN conversation_turns x ON x.task_id=t.id
                JOIN conversations c ON c.id=x.conversation_id
                WHERE t.id=? AND t.lease_token=? AND t.state='working'
                  AND t.lease_expires_at>? AND x.conversation_id=?
                  AND x.state='submitted' AND c.state='open'""",
                (task_id, lease_token, now(), cid)).fetchone()
            if not row:
                return False
            usage = db.execute("""SELECT COUNT(*) n,COALESCE(SUM(LENGTH(payload_json)),0) bytes
                FROM conversation_stream WHERE conversation_id=? AND task_id=? AND kind='delta'""",
                (cid,task_id)).fetchone()
            if usage["n"] >= 256 or usage["bytes"] + len(text.encode("utf-8")) > 80000:
                raise ValueError("Live-stream per-task quota exceeded")
            self._feed(db, cid, "delta", {"task_id":task_id,"attempt":row["attempts"],
                                            "text":text,"provisional":True}, task_id=task_id)
            return True

    def _event(self, db, conversation_id: str, kind: str, detail: str):
        db.execute("INSERT INTO conversation_events(conversation_id,kind,detail,created_at) VALUES(?,?,?,?)",
                   (conversation_id, kind, detail[:180], now()))
        self._feed(db, conversation_id, "state", {"kind":kind,"detail":detail[:180]})

    def create(self, *, owner: str, title: str, participants: list[str], project_id: str | None,
               max_turns: int) -> dict:
        if len(set(participants)) != len(participants):
            raise ValueError("Conversation participants must be unique")
        if project_id is not None and not _PROJECT.fullmatch(project_id):
            raise ValueError("Invalid project ID")
        cid = str(uuid.uuid4())
        stamp = now()
        with self.store.connect() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (cid, owner, project_id, title, "open", max_turns, 0, 0, stamp, stamp))
            for i, agent in enumerate(participants):
                db.execute("INSERT INTO conversation_participants VALUES(?,?,?)", (cid, agent, i))
            self._event(db, cid, "created", "Conversation created")
        return self.get(cid)

    def get(self, cid: str) -> dict | None:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if not row:
                return None
            participants = db.execute("SELECT agent_id FROM conversation_participants WHERE conversation_id=? ORDER BY ordinal", (cid,)).fetchall()
            turns = db.execute("SELECT number,agent_id,task_id,state FROM conversation_turns WHERE conversation_id=? ORDER BY number", (cid,)).fetchall()
        return dict(row) | {"participants": [r["agent_id"] for r in participants], "turns": [dict(r) for r in turns]}

    def list(self, owner: str | None = None, limit: int = 50) -> list[dict]:
        with self.store.connect() as db:
            if owner is None:
                rows = db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT id FROM conversations WHERE owner=? ORDER BY updated_at DESC LIMIT ?", (owner, limit)).fetchall()
        return [self.get(row["id"]) for row in rows]

    def messages(self, cid: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT seq,sender,recipient,role,content,task_id,created_at FROM conversation_messages
                WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?
            """, (cid, after, limit)).fetchall()]

    def events(self, cid: str) -> list[dict]:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT id,kind,detail,created_at FROM conversation_events
                WHERE conversation_id=? ORDER BY id LIMIT 1000
            """, (cid,)).fetchall()]

    def _insert_message(self, db, cid, sender, recipient, role, content, request_key=None,
                        request_hash=None, task_id=None):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM conversation_messages WHERE conversation_id=?", (cid,)).fetchone()[0]
        db.execute("INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (cid, seq, sender, recipient, role, content, request_key, request_hash, task_id, now()))
        self._feed(db, cid, "message", {"seq":seq,"sender":sender,"recipient":recipient,
                                           "role":role,"content":content,"task_id":task_id}, task_id=task_id)
        return seq

    def post(self, cid: str, *, sender: str, to: str, text: str,
             request_key: str | None = None, auto_rounds: int = 0) -> dict:
        if not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Message must be non-empty and at most 4096 bytes")
        digest = hashlib.sha256(canonical({"to": to, "text": text, "auto_rounds": auto_rounds}).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if not conversation:
                raise LookupError("Conversation not found")
            # Exact replay is safe even if the conversation subsequently closed.
            if request_key:
                previous = db.execute("SELECT seq,request_hash FROM conversation_messages WHERE conversation_id=? AND sender=? AND request_key=?", (cid, sender, request_key)).fetchone()
                if previous:
                    if previous["request_hash"] != digest:
                        raise ValueError("Idempotency key reused with a different message")
                    return {"conversation_id": cid, "message_seq": previous["seq"], "replayed": True}
            if conversation["state"] != "open":
                raise ValueError("Conversation is closed")
            participants = [r[0] for r in db.execute("SELECT agent_id FROM conversation_participants WHERE conversation_id=? ORDER BY ordinal", (cid,))]
            if to not in participants:
                raise ValueError("Agent is not a conversation participant")
            if db.execute("SELECT 1 FROM conversation_turns WHERE conversation_id=? AND state IN ('queued','submitted') LIMIT 1", (cid,)).fetchone():
                raise ValueError("Conversation already has an active turn")
            if conversation["auto_remaining"]:
                raise ValueError("Autonomous discussion is already running")
            remaining = conversation["max_turns"] - conversation["requested_turns"]
            if remaining < 1 or (auto_rounds and auto_rounds > remaining):
                raise ValueError("Conversation turn budget exhausted")
            if auto_rounds and auto_rounds > 20:
                raise ValueError("Discussion exceeds 20 rounds")
            seq = self._insert_message(db, cid, sender, to, "user" if sender.startswith("user:") else "agent",
                                       text, request_key, digest)
            turn_number = conversation["requested_turns"] + 1
            db.execute("INSERT INTO conversation_turns VALUES(?,?,?,?,?,?,?)",
                       (cid, turn_number, to, seq, None, "queued", now()))
            db.execute("UPDATE conversations SET requested_turns=?,auto_remaining=?,updated_at=? WHERE id=?",
                       (turn_number, max(0, auto_rounds - 1), now(), cid))
            self._event(db, cid, "message", f"Turn {turn_number} requested for {to}")
        return {"conversation_id": cid, "message_seq": seq, "turn": turn_number, "replayed": False}

    def close(self, cid: str) -> dict:
        task_ids = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone():
                raise LookupError("Conversation not found")
            db.execute("UPDATE conversations SET state='closed',auto_remaining=0,updated_at=? WHERE id=?", (now(), cid))
            turns = db.execute("SELECT task_id FROM conversation_turns WHERE conversation_id=? AND state IN ('queued','submitted')", (cid,)).fetchall()
            task_ids = [r["task_id"] for r in turns if r["task_id"]]
            db.execute("UPDATE conversation_turns SET state='canceled' WHERE conversation_id=? AND state IN ('queued','submitted')", (cid,))
            self._event(db, cid, "closed", "Conversation closed; remote agent side-effects may continue")
        for task_id in task_ids:
            self.store.cancel(task_id)
        return self.get(cid)

    def _finish_turn(self, turn: dict, task: dict):
        cid = turn["conversation_id"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT state FROM conversation_turns WHERE conversation_id=? AND number=?", (cid, turn["number"])).fetchone()
            if not active or active["state"] != "submitted":
                return
            conv = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            outcome = task["state"]
            if conv["state"] == "closed":
                outcome = "canceled"
            if outcome == "completed":
                try:
                    result = json.loads(task["output_json"])
                    content = _text(result)
                except (ValueError, TypeError):
                    content = "[unreadable agent output]"
                seq = self._insert_message(db, cid, f"agent:{turn['agent_id']}", None, "agent",
                                           content, task_id=task["id"])
            else:
                seq = None
            db.execute("UPDATE conversation_turns SET state=? WHERE conversation_id=? AND number=?",
                       (outcome, cid, turn["number"]))
            self._event(db, cid, outcome, f"Turn {turn['number']} {outcome}")
            if outcome == "completed" and conv["auto_remaining"] > 0 and conv["requested_turns"] < conv["max_turns"]:
                participants = [r[0] for r in db.execute("SELECT agent_id FROM conversation_participants WHERE conversation_id=? ORDER BY ordinal", (cid,))]
                idx = (participants.index(turn["agent_id"]) + 1) % len(participants)
                next_agent = participants[idx]
                number = conv["requested_turns"] + 1
                db.execute("INSERT INTO conversation_turns VALUES(?,?,?,?,?,?,?)",
                           (cid, number, next_agent, seq, None, "queued", now()))
                db.execute("UPDATE conversations SET requested_turns=?,auto_remaining=auto_remaining-1,updated_at=? WHERE id=?",
                           (number, now(), cid))
                self._event(db, cid, "auto_routed", f"Turn {number} routed to {next_agent}")
            elif outcome != "completed":
                db.execute("UPDATE conversations SET auto_remaining=0,updated_at=? WHERE id=?", (now(), cid))
            else:
                db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))

    def _dispatch(self, turn: dict):
        cid = turn["conversation_id"]
        with self.store.connect() as db:
            conv = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if conv is None or conv["state"] != "open":
                return
            source = db.execute("SELECT content FROM conversation_messages WHERE conversation_id=? AND seq=?", (cid, turn["source_seq"])).fetchone()
            if not source:
                return
            history = [dict(row) for row in db.execute("""
                SELECT sender,recipient,role,substr(content,1,1600) AS content
                FROM conversation_messages WHERE conversation_id=? AND seq<=?
                ORDER BY seq DESC LIMIT ?
            """, (cid, turn["source_seq"], MAX_CONTEXT_MESSAGES)).fetchall()]
            history.reverse()
        if turn["agent_id"] in self.blocked_agents:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE conversation_turns SET state='failed' WHERE conversation_id=? AND number=? AND state='queued'",
                                     (cid, turn["number"]))
                if changed.rowcount:
                    db.execute("UPDATE conversations SET auto_remaining=0,updated_at=? WHERE id=?", (now(), cid))
                    self._event(db, cid, "blocked", "Human approval required; conversation dispatch denied")
            return
        context = {"_mesh_conversation_id": cid, "_a2a_context_id": cid,
                   "_mesh_history": history}
        task, _ = self.store.create(agent_id=turn["agent_id"], owner=conv["owner"],
                                    input_value=source["content"], context=context,
                                    request_key=f"conversation:{cid}:{turn['number']}",
                                    parent_id=None, max_depth=5)
        cancel_after_commit = False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT state FROM conversation_turns WHERE conversation_id=? AND number=?", (cid, turn["number"])).fetchone()
            conv_now = db.execute("SELECT state FROM conversations WHERE id=?", (cid,)).fetchone()
            if state and state["state"] == "queued" and conv_now and conv_now["state"] == "open":
                db.execute("UPDATE conversation_turns SET task_id=?,state='submitted' WHERE conversation_id=? AND number=?",
                           (task["id"], cid, turn["number"]))
                self._event(db, cid, "dispatched", f"Turn {turn['number']} submitted")
            elif not conv_now or conv_now["state"] != "open":
                cancel_after_commit = True
            # Another scheduler may already have claimed the same idempotent task;
            # do not cancel it just because this tick lost the submission race.
        if cancel_after_commit:
            self.store.cancel(task["id"])

    def tick(self):
        """Reconcile through durable task IDs; safe for repeated calls/restarts."""
        with self.store.connect() as db:
            running = [dict(r) for r in db.execute("""
                SELECT * FROM conversation_turns WHERE state='submitted'
                ORDER BY created_at LIMIT 80
            """).fetchall()]
        for turn in running:
            task = self.store.get(turn["task_id"])
            if task and task["state"] in ("completed", "failed", "canceled"):
                self._finish_turn(turn, task)
        with self.store.connect() as db:
            queued = [dict(r) for r in db.execute("""
                SELECT * FROM conversation_turns WHERE state='queued'
                ORDER BY created_at LIMIT 80
            """).fetchall()]
        for turn in queued:
            self._dispatch(turn)


def install_conversation_api(app, engine: ConversationEngine, identity, registry, can_use, check_project):
    def visible(cid: str, profile: dict) -> dict:
        conv = engine.get(cid)
        if not conv:
            raise HTTPException(404, "Conversation not found")
        if conv["project_id"]:
            check_project(conv["project_id"], profile)
        agent_id = profile.get("agent_id")
        if profile.get("role") != "admin" and conv["owner"] != profile["name"] and not (agent_id and agent_id in conv["participants"]):
            raise HTTPException(404, "Conversation not found")
        return conv

    @app.post("/v1/conversations", status_code=201)
    def create_conversation(body: ConversationCreate, profile: dict = Depends(identity)):
        if profile.get("agent_id"):
            raise HTTPException(403, "Agent principals cannot create conversations")
        if body.project_id:
            check_project(body.project_id, profile, write=True)
        for agent in body.participants:
            if agent not in registry or not can_use(profile, agent):
                raise HTTPException(403, "Participant not allowed")
            if registry[agent].requires_approval:
                raise HTTPException(403, "Agent requires approved workflow; conversational bypass denied")
        try:
            return engine.create(owner=profile["name"], title=body.title,
                                 participants=body.participants, project_id=body.project_id,
                                 max_turns=body.max_turns)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/v1/conversations")
    def list_conversations(profile: dict = Depends(identity)):
        return engine.list(None if profile.get("role") == "admin" else profile["name"])

    @app.get("/v1/conversations/{cid}")
    def get_conversation(cid: str, profile: dict = Depends(identity)):
        return visible(cid, profile)

    @app.get("/v1/conversations/{cid}/messages")
    def list_messages(cid: str, after: int = 0, limit: int = 100,
                      profile: dict = Depends(identity)):
        visible(cid, profile)
        if not (0 <= after and 1 <= limit <= 100):
            raise HTTPException(422, "Invalid pagination")
        return engine.messages(cid, after=after, limit=limit)

    @app.get("/v1/conversations/{cid}/events")
    def conversation_events(cid: str, profile: dict = Depends(identity)):
        visible(cid, profile)
        return engine.events(cid)

    @app.post("/v1/conversations/{cid}/messages", status_code=202)
    def send_message(cid: str, body: ConversationMessage, profile: dict = Depends(identity)):
        conv = visible(cid, profile)
        if not can_use(profile, body.to) or registry[body.to].requires_approval:
            raise HTTPException(403, "Agent not allowed without approved workflow")
        if profile.get("agent_id") == body.to:
            raise HTTPException(403, "Agent cannot self-message")
        sender = f"agent:{profile['agent_id']}" if profile.get("agent_id") else f"user:{profile['name']}"
        try:
            return engine.post(cid, sender=sender, to=body.to, text=body.text,
                               request_key=body.idempotency_key)
        except LookupError:
            raise HTTPException(404, "Conversation not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/v1/conversations/{cid}/discussions", status_code=202)
    def start_discussion(cid: str, body: ConversationDiscussion, profile: dict = Depends(identity)):
        conv = visible(cid, profile)
        if profile.get("agent_id"):
            raise HTTPException(403, "Autonomous discussions require a human principal")
        if any(not can_use(profile, agent) or registry[agent].requires_approval for agent in conv["participants"]):
            raise HTTPException(403, "Agent not allowed without approved workflow")
        if len(conv["participants"]) < 2:
            raise HTTPException(422, "Discussion needs two or more agents")
        try:
            return engine.post(cid, sender=f"user:{profile['name']}", to=conv["participants"][0],
                               text=body.prompt, request_key=body.idempotency_key,
                               auto_rounds=body.rounds)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/v1/conversations/{cid}/stream")
    async def stream_conversation(cid: str, request: Request, after: int = 0, wait_seconds: float = 20.0,
                                  last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                                  profile: dict = Depends(identity)):
        # Authorize before sending any stream bytes, never trust a client-supplied sender.
        visible(cid, profile)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
        except (TypeError, ValueError):
            raise HTTPException(422, "Invalid event cursor") from None
        if not 0 <= cursor <= 2**63 - 1 or not 0.1 <= wait_seconds <= 30:
            raise HTTPException(422, "Invalid event cursor")

        async def generate():
            end = time.monotonic() + wait_seconds  # bounded connection; clients reconnect by cursor
            last_ping = time.monotonic()
            nonlocal cursor
            while time.monotonic() < end:
                if await request.is_disconnected():
                    return
                rows = engine.stream_events(cid, after=cursor, limit=100)
                for row in rows:
                    cursor = row["seq"]
                    payload = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {row['kind']}\ndata: {payload}\n\n"
                if rows:
                    continue
                current = engine.get(cid)
                if not current or current["state"] == "closed":
                    return
                if time.monotonic() - last_ping >= 3:
                    yield ": keepalive\n\n"
                    last_ping = time.monotonic()
                await asyncio.sleep(0.10)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control":"no-cache, no-transform",
                                          "X-Accel-Buffering":"no", "X-Content-Type-Options":"nosniff"})

    @app.post("/v1/conversations/{cid}/close")
    def close_conversation(cid: str, profile: dict = Depends(identity)):
        conv = visible(cid, profile)
        if profile.get("agent_id") or (profile.get("role") != "admin" and profile["name"] != conv["owner"]):
            raise HTTPException(403, "Only human owner/admin can close conversation")
        return engine.close(cid)
