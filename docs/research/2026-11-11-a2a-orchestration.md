# 2026-11-11 — A2A 与工作流协调边界

## Sources and status
- A2A 1.0 published specification (Agent2Agent project): https://a2a-protocol.org/v1.0.0/specification
- Protocol source repository: https://github.com/a2aproject/A2A
- Local project baseline: Open Agent Mesh v0.1.0; implementation tested on 2026-11-11.

## Evidence and interpretation
A2A standardizes agents exchanging messages and checking tasks (e.g., SendMessage, GetTask). It does not prescribe organizational DAG policy, authorization for all downstream agents, cross-agent dependency approval, or shared project memory.

## Decision
Develop DAG orchestration and project knowledge policy as Mesh-owned features layered above the A2A/HTTP transports. Keep A2A JSON-RPC compatibility explicitly partial. Do not present Mesh workflow endpoints as native A2A methods.

## Alternatives and next validation
A full A2A SDK adapter and conformance suite are future work; compare with agent-framework-native planners and distributed queues. Add crash/fault-injection tests, outbox/leases, human approval gates, cost caps, and formal trust policy before public production claims. Interoperability with named third-party products remains unverified until authenticated integration tests succeed.
