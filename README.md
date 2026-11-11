# Open Agent Mesh 0.3.0

**让不同 Agent 在一个统一网络内交换任务。** A small, framework-independent, runnable multi-agent task gateway.

**Status: proof-of-concept MVP.** It is NOT a production-ready distributed coordinator or a fully certified A2A protocol implementation. **Only a limited A2A 1.0 JSON-RPC binding is supported.**

## Features already implemented

- Agents are registered in an admin-maintained JSON config: Python handlers, generic HTTP JSON endpoints, or remote A2A 1.0 JSON-RPC agents.
- Authenticated tasks: submit, query, cancel, audit, and enumerate accessible agents.
- Durable SQLite task queue with bounded execution, expiring leases, heartbeat, and stale-worker fencing; single-process DAG scheduler remains mandatory.
- Per-principal agent ACL, task ownership isolation, idempotency keys, delegation depth limit.
- Per-agent A2A 1.0 JSON-RPC endpoint and discoverable Agent Card.
- Basic A2A methods: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask`; `returnImmediately` behavior; text/data parts.
- Python client and Node.js client; example webhook adapter.
- Optional **Agent Skills**-format `SKILL.md` with a zero-dependency CLI for supported coding agents.
- Versioned **shared project facts** with explicit project permissions and optimistic-concurrency revision checks.

**v0.4:** Human-gated workflow approvals and stateless MCP 2026-07-28 JSON-response tool bridge (a limited subset). See [`docs/RELIABILITY-v0.3.zh-CN.md`](docs/RELIABILITY-v0.3.zh-CN.md).

**Not yet implemented:** SSE streaming, push callbacks, A2A task continuation/multi-turn, file parts, Agent Card signatures, OAuth/OIDC, cross-org policies, externally verified human identity, distributed DAG ownership, retry/dead-letter queue, semantic skill matching, shared memory, cost budgeting, multi-tenancy, full protocol conformance, or native ChatGPT/Cursor/Claude integrations. Unsupported features are not advertised in Agent Cards.

## Quick start

Requires Python 3.10+. On a Mac/Windows/Linux terminal:

```bash
cd open-agent-mesh
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
export MESH_TOKENS='{"local-demo-admin-token-change-me":{"name":"admin","role":"admin","allowed_agents":["*"]},"local-demo-client-token-change-me":{"name":"alice","allowed_agents":["echo","uppercase"],"allowed_projects":["demo-project"]}}'
export MESH_TOKEN='local-demo-client-token-change-me'
export MESH_DB_PATH='./mesh.sqlite3'
agentmesh
```

In another terminal (with the same environment/token):

```bash
curl -s -H "Authorization: Bearer $MESH_TOKEN" http://127.0.0.1:8765/v1/agents
python examples/python_client.py
node examples/node_client.mjs
```

Or submit an asynchronous task with cURL:

```bash
curl -s http://127.0.0.1:8765/v1/tasks \
  -H "Authorization: Bearer $MESH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"to":"uppercase", "input":"hello agent mesh", "idempotency_key":"demo-001"}'
```

Retrieve the returned `id` via `GET /v1/tasks/{id}`. Local Swagger docs: `http://127.0.0.1:8765/docs`.

**Security:** These example tokens are intentionally identifiable demos, **not for public deployment**. Generate random 32+ byte secrets. Never commit actual tokens. Bind only to 127.0.0.1 for local demo. Put a production service behind HTTPS and proper authentication, rate-limiting, centralized secrets, firewall, API gateway, and robust task workers.

## A2A 1.0 JSON-RPC call

Discovery: `GET http://127.0.0.1:8765/agents/uppercase/.well-known/agent-card.json` (public card; authenticated invocation).

```bash
curl -s http://127.0.0.1:8765/a2a/uppercase \
 -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
 -H "Authorization: Bearer $MESH_TOKEN" \
 -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"my-unique-msg-123","role":"ROLE_USER","parts":[{"text":"hello!"}]},"configuration":{"returnImmediately":false}}}'
```

Default call waits for completion. `returnImmediately=true` returns a task to poll with `GetTask`. The A2A v1 naming (`SendMessage`, `ROLE_USER`, `TASK_STATE_COMPLETED`) **differs from A2A 0.3** (`message/send`, `user`, etc.). Clients must send `A2A-Version: 1.0`. The mesh does **not** support v0.3. It implements a subset of A2A 1.0; use the official SDK where comprehensive conformance is required.

## Connect your agent

Edit `agents.example.json` yourself, or point `MESH_AGENTS_CONFIG` to a separate trusted JSON file.

### Python callable

```json
{"id":"my-agent", "kind":"python", "handler":"my_module:run", "name":"My Agent", "description":"My job", "skills":["analysis"]}
```

Your callable receives `(input, context)` and returns `str` or JSON-compatible data. It can be `async def` or synchronous. Because imports run arbitrary code, only administrators may modify the agent configuration.

### Arbitrary HTTP/JSON agent

```json
{"id":"my-agent", "kind":"http_json", "url":"https://my-agent.example.com/invoke", "token_env":"MY_AGENT_TOKEN", "name":"My Agent", "description":"My job", "skills":["research"]}
```

Outgoing `POST` payload:

```json
{"task_id":"mesh-task-uuid", "input":"analyze this", "context":{}, "parent_task_id":null}
```

Return `{"output":"final result"}` (or any JSON-serializable value). The mesh passes a stable `Idempotency-Key` HTTP header; implement receiver-side deduplication before side effects.

### Existing A2A agent

```json
{"id":"remote-agent", "kind":"a2a_jsonrpc", "url":"https://remote-agent.example.com/a2a/jsonrpc", "token_env":"REMOTE_A2A_TOKEN", "name":"Remote Agent", "description":"Remote analysis", "skills":["research"]}
```

Remote agent must actually expose the A2A **1.0 JSON-RPC binding**. This sample calls `SendMessage` and polls `GetTask` when necessary. It does not support a gRPC-only agent or A2A 0.3-only agent. HTTP URLs are admin-supplied and should be HTTPS outside loopback.

## Share project decisions (explicit opt-in)

A token profile can include `"allowed_projects": ["demo-project"]` (admins can access all projects). Authorized agents can collaboratively read/write facts; this is **explicit project-wide storage**, not implicit access to private model memory.

```bash
curl -s -X PUT http://127.0.0.1:8765/v1/projects/demo-project/facts/business-rule \
  -H "Authorization: Bearer $MESH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"value":{"rule":"核销通过后才能请付款"}}'
```

The `expected_revision` is 0 for new facts, otherwise the current version. Stale writes return HTTP 409, avoiding silent overwrites by concurrent agents. History endpoint: `GET /v1/projects/{project}/facts/{key}/history`. **Grant `allowed_projects` only to agents that may read all knowledge in that project**; project facts are not encrypted and must not contain credentials or private data. Facts are not automatically attached to task inputs.

## Cross-agent Skill installation

`skills/open-agent-mesh/SKILL.md` is a portable Agent Skills instruction package. Install its directory into a compatible tool's project-specific skill location, such as `.agents/skills/open-agent-mesh/` (Codex and Cursor) or `.claude/skills/open-agent-mesh/` (Claude Code). These agents must also have an authorized terminal/Python environment and an accessible running gateway. Installation paths and cloud synchronization vary by product. Example:

```bash
mkdir -p .agents/skills
cp -R skills/open-agent-mesh .agents/skills/open-agent-mesh
# After setting MESH_TOKEN: python3 .agents/skills/open-agent-mesh/scripts/mesh.py agents
```

This does **not** automatically integrate with the hosted ChatGPT or Claude consumer chat interfaces; those require their own enabled tool connector/MCP bridge or host-supported skill loader.

## Coordination boundaries

Each task has a `parent_task_id` and finite depth to limit uncontrolled recursion. The core itself does not prompt other Agent models or choose agents intelligently; a coordinator agent can call `GET /v1/agents`, choose an allowed destination, then call `POST /v1/tasks` and poll. Pass only the task context intended for the receiving agent. No implicit sharing of secrets or private prompts.

Data isolation: Bearer tokens resolve to principals; non-admin users can read only their own tasks and are authorized per destination. Shared project facts require explicit allowed_projects; all project members can see the project's facts. **This is a starter authorization model, not enterprise multi-tenancy.** For A2A remote messaging use separate service credentials stored in `token_env` variables.

One process is required with this MVP SQLite queue. Task leases and fencing are implemented for SQLite, but **multi-replica DAG coordination and exactly-once remote execution are not guaranteed**. Because a crash may replay a task, make external actions idempotent. Cancellation changes local task state; it does not forcibly stop an already-started external agent or undo side effects.

## Run tests

```bash
PYTHONPATH=src pytest -q
```

See `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` and `examples/` for limitations and extension points. For live network integration run `PYTHONPATH=src python tests/smoke_network.py`.

## References

- Official A2A 1.0 specification: https://a2a-protocol.org/v1.0.0/specification/
- Official A2A Python SDK: https://github.com/a2aproject/a2a-python
- Official A2A samples: https://github.com/a2aproject/a2a-samples

This project is independent and is **not affiliated with Cursor or the A2A maintainers**.


## v0.2: persisted workflow coordinator

The `POST /v1/workflows` endpoint accepts up to 24 explicitly authorized steps, with `depends_on`, nested `{"$from":"step"}` output references, bounded retries (`max_attempts` 1–3), and optional project fact keys pinned by revision at submission. Independent branches are dispatched concurrently; failed dependencies skip their dependents. Workflow state and events survive process restarts on the existing SQLite instance. See [`docs/WORKFLOWS.zh-CN.md`](docs/WORKFLOWS.zh-CN.md) and [`examples/workflow.json`](examples/workflow.json).

The caller remains responsible for choosing and authorizing the DAG; this is **not** autonomous LLM planning, full A2A conformance, durable distributed scheduling, or production-grade credential isolation. `project_roles` adds `reader` and `editor` fact permissions, with legacy `allowed_projects` remaining read-write.

## v0.3 test rounds

Run `python -m pytest -q`, `python tests/smoke_network.py`, `python tests/chaos_network.py`, and `python tests/stress_http.py` after `pip install -e ".[dev]"`. GitHub Actions repeats all four on Python 3.10, 3.12 and 3.13. The test plan is in [`docs/TEST_PLAN-v0.3.zh-CN.md`](docs/TEST_PLAN-v0.3.zh-CN.md). All demos use local loopback test tokens only; do not expose this MVP on the public Internet.


## v0.4 — Durable Agent Conversation Engine

**New:** multi-turn private conversations, authorized agent-to-agent posting, bounded rotating group discussions, durable transcripts, conversation-level audit events, idempotency, failure-stop and crash recovery. This is a first-party HTTP/MCP/Skill extension to the existing partial A2A 1.0 bridge—not full real-time chat or an A2A/MCP conformance certification.

```python
from agentmesh.client import MeshClient
mesh = MeshClient("http://127.0.0.1:8765", "YOUR_AUTHORIZED_TOKEN")
conv = mesh.create_conversation("Architecture review", ["uppercase", "echo"], max_turns=6)
mesh.start_discussion(conv["id"], "Review the memory model", rounds=4)
# Read conversation(conv['id']) and conversation_messages(conv['id']) until complete.
```

Agent identity is bound to the authenticated principal; high-risk gated agents cannot be called through a conversation. Automated relay only starts with explicit human instruction, is bounded to 2–20 turns and stops on failure. See [Chinese conversation design and API guide](docs/CONVERSATIONS-v0.4.zh-CN.md).
