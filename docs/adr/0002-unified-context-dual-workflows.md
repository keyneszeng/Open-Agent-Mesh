# ADR 0002 — Unified context and dual workflow modes

Date: 2026-11-11
Status: Accepted as v0.6 architecture direction; implementation pending

## Decision

Open Agent Mesh supports **two modes over one collaboration core**:

- **Direct Workflow**: an authorized initiator delegates tasks between agents without creating a persistent group or workspace. Every delegated task is bound to a goal and a governed context.
- **Workspace Workflow**: human and agent participants operate within a persistent membership scope, with roles, tasks, message history, shared decisions and artifacts. The workflow engine is the same; the workspace adds organization and visibility, not a second execution stack.

The **Unified Context Engine** is the canonical source of goal constraints and working facts. Conversation messages, task outputs, and self-reported agent assertions never silently replace authoritative context. A workspace is optional; a goal/context binding is not.

## Domain model (proposed; not yet deployed)

- `HumanPrincipal`: accountable user or organization identity, subject to actual authentication.
- `AgentIdentity`: a uniquely identifiable agent workload/provider adapter; cannot self-assert human identity.
- `DelegationGrant`: principal, agent identity, scope (actions/resources), expiry, approver where required, revocation state; non-transferable unless explicitly permitted.
- `Goal`: objective, sponsor, acceptance criteria, lifecycle status and context reference.
- `ContextBundle`: versioned rules, decisions, resources, facts and evidence references. Immutable revision hashes; changes audited.
- `ContextView`: per-agent authorized subset derived from one authoritative version; secrets excluded unless an explicit secure tool boundary requires them.
- `WorkflowRun`: direct or workspace-bound execution; same scheduling/lease/audit semantics and invariant context binding.
- `Workspace`: optional long-lived membership, participant roles, channel(s), resources and linked goals.
- `Task` / `Conversation` / `Artifact`: linked to goal, context revision, execution and human/agent provenance.

**Mode choice** is represented by `workspace_id = null | workspace-id` on a workflow; it must not fork business rules or authorization.

## Non-negotiable invariants

1. Every workflow execution records a `goal_id`, authoritative `context_id` and `context_revision`. No naked, unscoped agent execution in the future model.
2. The context revision is immutable for an execution; a human-authorized update produces a new revision and explicit rebasing, not silent mutation.
3. Context is a single source of truth but **not a global readable blob**. Context views are least-privilege projections, and resource authorization is enforced at every read/write/tool boundary.
4. The effective rights of a delegated task are the **intersection** of initiator grant, delegating agent scope, receiving agent scope, resource policy and active approvals. Forwarding a task cannot increase scope.
5. A human principal remains accountable; agent execution identity and delegation chain are always separately attributed in audit events.
6. High-risk effects (production deploy, payment, destructive data operations, privilege changes) require explicit human authority and applicable approval policy. Agent consensus does not substitute for approval.
7. Agent messages, external artifacts, tool results and project knowledge are untrusted input. Rules are validated by trusted services, not by following a prompt.
8. Direct workflow can be attached to a workspace without losing its goal, revision, task IDs, audit trail or idempotency keys; scope changes require authorization.
9. Revoked grants stop **new** actions at the authorization boundary. Ongoing remote effects may not be cancellable; surface that limitation.
10. Transport and adapters (A2A / MCP / HTTP / Skills) cannot bypass the common policy engine.

## Context change and provenance

Context changes should use compare-and-swap expected revision plus provenance: author identity, rationale, time, linked decision/approval, and impacted runs. Snapshot and redact context before dispatch; never copy secrets or entire unrelated conversations by default.

## Alternatives considered

- Make every collaboration a workspace: rejected; imposes a persistent team/group even for simple handoffs.
- Maintain separate direct and workspace orchestrators: rejected; creates divergent ACLs and replay behavior.
- Treat a chat transcript as the only memory: rejected; mutable/ambiguous and vulnerable to instruction injection.
- Give each Agent unrestricted access to one shared context: rejected; violates least privilege.

## Compatibility and staged rollout

v0.5 has partial task, conversation, shared-facts and ACL mechanisms. This decision **does not claim** v0.6 workspace identity models or unified execution binding already exist. Introduce schema/API changes incrementally, preserve existing API behavior where possible, and provide migration/feature gating. Release notes must identify breaking changes.

## Acceptance and evidence

Implement and test the cases defined in [v0.6 plan](../V0.6_DUAL_MODE_SPEC.zh-CN.md): direct handoff, workspace group, same context revision, per-agent projection, cross-mode promotion, delegation attenuation, revoked grant, optimistic conflict, failure recovery and human approval. Mark each case passed only after reproducible tests and GitHub CI.

## Follow-up risks

External provider adapters may not offer stable agent identity, signed claims or safe cancellation; multi-tenant auth, strong OIDC/mTLS, secure credential brokering, isolation, storage encryption, billable resource quotas and abuse mitigation remain separate production work.
