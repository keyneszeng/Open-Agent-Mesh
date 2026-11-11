# Product roadmap

- **v0.1 (this bundle):** portable API; basic A2A 1.0 JSON-RPC; generic HTTP/Python agent adapters; persist tasks; ACL, audit, idempotency, hop limits; project knowledge with revision history; portable Agent Skill; tests.
- **v0.2 (implemented initial slice):** persisted explicit DAG workflows, dependency-output passing, retries, snapshot of selected shared facts, reader/editor ACL, CLI/SDK endpoints and tests. Still pending: full A2A SDK adapter with conformance tests; streaming SSE, files, multi-turn continuation, push callbacks and cancellation propagation; structured capability schema.
- **v0.3 (implemented initial slice):** SQLite task leasing and restart recovery (not distributed DAG), mandatory human approval gates, partial MCP 2026-07-28 tool bridge, systematic failure injection / concurrent tests. **Pending:** OIDC/mTLS, signed Agent Cards, PostgreSQL + Redis, cross-tenant isolation, quotas, external conformance.
- **v0.4 (implemented initial slice):** multi-turn durable Conversation Engine, authenticated participant and project ACL, bounded 2–20 round discussions, A2A `contextId` carry-over, MCP/Skill conversation interface, crash and network test suite. **Pending:** `taskId` in-place continuation, external interoperability certification, OpenAI Agents SDK / LangGraph / CrewAI reference adapters, full MCP compliance.
- **v0.5 (implemented slice):** private event-stream SSE with replay, real-time HTTP and A2A streaming adapters, provisional delta persistence/fencing, A2A `SendStreamingMessage`/`SubscribeToTask` subset, SDK/Skill listeners and realistic fault/concurrency tests. Still missing actual provider SDKs, full protocol certification, long-term streaming retention policy and enterprise authorization.
- **v0.6 (planned, architecture approved 2026-11-11):** HumanPrincipal/AgentIdentity/DelegationGrant, versioned Unified Context Engine, two modes on one orchestration stack (**Direct Workflow** without workspace; **Workspace Workflow** with persistent membership), bounded rights delegation, audited promotion of a direct goal into a workspace. See [ADR 0002](adr/0002-unified-context-dual-workflows.md) and [acceptance plan](V0.6_DUAL_MODE_SPEC.zh-CN.md). Provider-native adapters, A2A/MCP conformance, adaptive cost/rate budgets, tracing/backpressure remain subsequent parallel tracks; none are claimed implemented.

**Architecture rule:** the protocol layer transports messages; the coordination layer decides *which* agent, *when*, *with what evidence*, and *under whose authorization*. Neither the wire protocol nor a model's claims substitute for authorization.

## Product architecture commitment: Context-first dual mode (ADR-0002; accepted 2026-11-11)

The product has **two equally first-class operating modes** sharing the same authoritative `goal/context_scope`, policy engine, identity/delegation chain, workflow scheduler, events and artifact journal:

- **Direct Flow (no workspace/group):** one goal, an authorized principal and a bounded inter-agent workflow; no persistent collaboration room required. It still retains durable context, audit and task history.
- **Workspace Flow (with workspace/group):** an ongoing group of human members and multiple heterogeneous agents, working on one or more goals through conversations and workflows; the workspace is an optional coordination container, **not** a second context store.

**Human agency:** all nontrivial Agent side effects must be attributable to an authenticated human or organization, an explicit bounded delegation and a scoped Agent identity. An Agent is acting *on behalf of* a principal; this does not imply one-to-one human/agent ownership. Human approval remains mandatory for high-risk operations.

**Context semantics:** one logical, versioned source of truth, **not unrestricted replication of private memory or secrets**. Agents receive only authorized context views pinned to a revision. Conversations are evidence/proposals; they cannot silently rewrite authoritative goals or policies.

**Implementation work, not yet completed:**
1. ContextEngine entities: goal, context_scope, revisions, decision provenance and scoped views; scope isolation and CAS updates.
2. Direct Flow: workflow/run API bound to goal without workspace; recoverability and auditing.
3. Workspace: membership, role assignment, multiple workflows, group conversations, shared goal/context and controlled history visibility.
4. Attach Direct Flow to Workspace without losing goal/context IDs, artifacts, events or authorization provenance.
5. Human ↔ Agent delegation registry, revocation, least-privilege grants, budget/approval gates.
6. Unit, property, concurrency, failure injection, data-leak, cross-process and official adapter interoperability tests.

Milestone proposal: prioritize context foundations and Direct Flow first, then workspace lifecycle and migrations, then external provider adapters and enterprise tenancy. The v0.1–v0.5 claimed features and existing v0.6 provider aspirations remain historically true, but **no dual-mode workspace behavior is yet implemented or tested**. Detailed contract: [ADR-0002](adr/0002-context-first-dual-mode-and-human-delegation.md).
