# Changelog

## 0.4.0 — 2026-11-11

- Agent Conversation Engine: persisted participants, owner/project ACL, attributed messages, finite turn budgets, manual and rotating agent discussions, history window, audit and close.
- SQLite task reconciliation and idempotent turn dispatch for restart recovery; stop on failed agent and deny approval-gated agents even after configuration changes.
- A2A `contextId` continuation through newly created tasks; cross-gateway A2A context propagation, bounded `_a2a_history`, same-message replay validation (in-place `taskId` continuation remains unsupported).
- MCP sub-protocol conversation tools, Python client and Skill CLI commands.
- New unit tests, cross-process A2A+HTTP conversation, SIGKILL conversation recovery and concurrent conversation load scripts.
- Limits: no real third-party proprietary agent integration, no push/SSE, no full MCP/A2A conformance, no multi-tenant production security or exactly-once external effects.


## 0.3.0 — 2026-11-11

- SQLite task lease and heartbeat, fencing token, lease expiry recovery, legacy database schema migration and explicit warning about at-least-once external side effects.
- Mandatory approval gates for configured agents: approve/reject endpoints with separate principal, project-scoped reviewer ACL and audit events.
- Partial MCP 2026-07-28 stateless JSON Streamable HTTP endpoint with five scoped tools; not a complete implementation.
- Error handling, health-check scheduler status, fixed DB connection cleanup, tests, chaos injection and concurrent load suite.
- New CI Python version matrix (3.10/3.12/3.13) with six verification stages.
- **Not yet production ready:** external OIDC, distributed DAG ownership, PostgreSQL, quotas, SSE, independent conformance certification.


## 0.2.0 — 2026-11-11

- Durable explicit DAG work orchestration: dependencies, partial parallelism, nested result references, bounded retries and cancellation.
- Workflow HTTP API, Python client and portable Agent Skill commands.
- Pinned project fact snapshots and `reader`/`editor` project roles (legacy `allowed_projects` retained).
- 16 new workflow tests and existing 24 regression tests (40 total), plus cross-process smoke test.
- Scope: single process SQLite, no automatic LLM planning, no distributed lease, not full A2A conformance.

## 0.1.0 — 2026-11-11

- Initial A2A 1.0 JSON-RPC subset, HTTP and Python adapters, durable tasks, scoped facts, token ACL, examples and test suite.
