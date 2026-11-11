# Architecture and threat model (v0.1)

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
- *Durability*: sqlite stores submitted, working, completed, failed and canceled tasks. On restart, interrupted `working` becomes `submitted` (possible replay!).
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
5. One process; four in-flight jobs; durable local DB. Not distributed-safe; external side effects may run twice after a crash.

## Missing before Internet/public production

Externalized identity (OIDC / mTLS); multi-tenant isolation; quotas and rate limits; durable work leases and retry/DLQ; per-action human approvals; secure artifact storage; TLS; signed/verifiable Agent Cards; network egress policy; DNS pinning; full A2A SDK compliance testing; observability/tracing; payload size limit at reverse proxy; prompt-injection protections in delegated content; test against third-party agent implementations.
