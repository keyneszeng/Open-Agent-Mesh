# Architecture and threat model (v0.3)

```mermaid
flowchart LR
  A[Coordinator / arbitrary agent] -->|Bearer HTTP JSON or A2A 1.0| B[Open Agent Mesh API]
  B --> C[Access policy and idempotency]
  C --> D[(SQLite tasks and event journal)]
  D --> E[Async worker]
  E --> F[Local Python handler]
  E --> G[HTTP JSON agent]
  E --> H[Remote A2A 1.0 agent]
```

- *Discovery*: `GET /v1/agents` (authorized) and per-agent public Agent Cards.
- *Delegation*: HTTP task with destination, context, parent ID; fixed-depth recursion bound.
- *Durability*: sqlite stores submitted, working, completed, failed and canceled tasks. Expired `working` leases are atomically re-queued by the next claim; an active lease is not stolen just because another instance initialized. External replay remains possible.
- *Authorization*: one opaque bearer credential per principal; `allowed_agents` controls dispatch; owner scopes task retrieval.
- *Secret handling*: remote auth token only read from environment; never store remote token in task body. Context is passed through verbatim; caller must exclude secrets.
- *Shared knowledge*: Project-scoped facts with optimistic concurrency and complete revisions; all explicitly authorized project members can read them.
- *Audit*: recorded task state transitions without user input/body. Tasks themselves do store input and output, unencrypted on disk; use disk encryption and retention policy.
- *Conformance*: this gateway implements a **partial** A2A 1.0 JSONRPC binding. Unsupported protocol operations are explicitly rejected.

## Deliberate constraints

1. No public URL registration: prevents unauthenticated clients turning the gateway into a general-purpose SSRF proxy.
2. No uncontrolled agent tool execution from natural-language prompts: only configured destinations are callable.
3. No default authorization token: missing `MESH_TOKENS` aborts startup.
4. No implicit access to one another's private memory, prompts, credentials or files.
5. Single-process DAG scheduler, four in-flight tasks per process. SQLite task claims have leases but multi-process DAG coordination remains unsupported; external side effects may run twice after a crash.

## Missing before Internet/public production

Externalized identity (OIDC / mTLS); multi-tenant isolation; quotas and rate limits; durable work leases and retry/DLQ; per-action human approvals; secure artifact storage; TLS; signed/verifiable Agent Cards; network egress policy; DNS pinning; full A2A SDK compliance testing; observability/tracing; payload size limit at reverse proxy; prompt-injection protections in delegated content; test against third-party agent implementations.


## v0.2 persisted coordinator

`WorkflowEngine` records explicit DAGs in `workflows`, `workflow_steps`, and `workflow_events` alongside the existing task journal. One background loop polls DAGs and submits ready steps via the same authenticated task storage path, with deterministic workflow step attempt idempotency keys. Deployed as a single process; **do not enable multiple server worker processes**. A crash between an external agent effect and task status persistence can still cause replay. Only project facts explicitly named by `fact_keys` are copied (revision-pinned) into a step context. Legacy `allowed_projects` grants editor access; fine-grained `project_roles` supports reader/editor. Store content is not encrypted at rest.


## v0.3 upgrade slice

SQLite claims now use expiry + fencing tokens and heartbeat; sqlite connections are closed rather than merely committed. Human-gated agents are configured server-side and do not accept direct tasks. A partial MCP 2026-07-28 JSON-response bridge shares the existing bearer token ACL. Reviewers must be a different principal from the workflow owner and have explicit review authority. A review token must NOT be accessible from Agent code. See [v0.3 reliability details](RELIABILITY-v0.3.zh-CN.md) and [test plan](TEST_PLAN-v0.3.zh-CN.md).


## v0.4 Conversation Engine

```mermaid
flowchart TD
  HUMAN[Human authorized initiator] --> API[Conversation REST / MCP / Skill API]
  AGENT[Authorized participant Agent] --> API
  API --> ACL[Authenticated owner / project / participant ACL]
  ACL --> CONV[(SQLite conversation + turns + messages + audit)]
  CONV --> TICK[Deterministic bounded scheduler]
  TICK --> TASK[(Existing durable task/lease queue)]
  TASK --> A2A[A2A 1.0 subset]
  TASK --> HTTP[HTTP JSON]
  TASK --> LOCAL[Python function]
  TASK --> CONV
```

Conversation turns use stable idempotency keys, avoid arbitrary LLM tool execution, and stop auto relay on failed worker tasks. Only a human principal can initiate a bounded autonomous discussion or close the conversation. Agent claims are derived from bearer credential `agent_id`, not user-supplied role. See [conversation specification](CONVERSATIONS-v0.4.zh-CN.md).
