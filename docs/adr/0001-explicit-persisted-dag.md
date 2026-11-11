# ADR 0001: Explicit persisted DAG coordinator

Date: 2026-11-11
Status: Accepted for v0.2 prototype; review before distributed deployment

## Context
A2A defines interoperability, not which agent should be authorized to act or which task dependency must complete first. The v0.1 gateway stores individual tasks but depends on an external manual coordinator for a sequence of work.

## Decision
Store a bounded caller-approved DAG in SQLite. The gateway validates agent and project authorization before acceptance. Steps may reference only explicitly declared dependency outputs. Ready steps are sent through the existing idempotent task journal, with deterministic attempt keys. Store selected project facts by revision at workflow submission. A single process polls durable DAGs.

## Alternatives
- Model-generated runtime plans: deferred until explicit approval policies and evals exist.
- Distributed queue and per-step leases: desirable for scale but adds operational complexity and requires replay/lease correctness tests.
- Pushing DAG semantics into A2A: rejected; orchestration is a Mesh-level capability, not part of the peer-agent wire contract.

## Consequences and risks
- Supports independent parallel branches and process restart visibility without another service.
- Step retry may cause repeated external side effects; remote agents must implement idempotent handling.
- Cancellation only stops local task state and does not guarantee termination of remote effects.
- No multi-worker exclusion, tenant encryption or reliable distributed scheduling. Run only one server worker for v0.2.
- Existing allowed_projects entries continue to confer read-write access; use project_roles for reader/editor separation.

## Validation
40 local pytest cases and a cross-process A2A/HTTP/Node/Skill smoke test. No third-party A2A certification or production fault injection claimed.
