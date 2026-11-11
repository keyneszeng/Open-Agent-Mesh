---
name: open-agent-mesh
description: Coordinate tasks across authorized local or remote AI agents through the Open Agent Mesh REST or A2A 1.0 gateway. Use when you need to discover another agent, delegate an explicitly scoped task, check its status, or read/update shared project decisions.
license: MIT
compatibility: Requires Python 3.10+ and network access to a running Open Agent Mesh server. The user or administrator must configure MESH_URL and MESH_TOKEN. No automatic access to external agents.
metadata:
  version: '0.2.0'
---

# Open Agent Mesh skill

Use this skill ONLY after verifying the local project has access to a running Open Agent Mesh instance and a configured bearer credential. Do not ask for, print, commit, or expose secret tokens in the conversation or source code. Credentials must come from environment variables.

## Setup and boundaries

- Service: `MESH_URL` (defaults to `http://127.0.0.1:8765` for local demos); credential: `MESH_TOKEN`.
- Python tool script: `scripts/mesh.py`. Run from this skill directory or use an absolute script path.
- This skill is client guidance, not a privileged bridge into your model provider. It cannot grant new tool permissions or control agents without authorization.
- Don't assume all destinations can read private context. Pass only the minimum necessary data.
- Do not delegate deployments, payments, deletions, or access/permission changes without an explicit human approval workflow outside this demo gateway.

## Workflow

1. Check available agents and descriptions: `python3 scripts/mesh.py agents`.
2. Select one authorized destination based on capability. If uncertain, ask which destination to use rather than using an unrelated agent.
3. Delegate narrowly: `python3 scripts/mesh.py send uppercase 'hello world'`.
4. The command returns a task ID and its current status. To retrieve the final outcome: `python3 scripts/mesh.py get TASK_ID`.
5. If coordinating a team project, read explicit knowledge: `python3 scripts/mesh.py facts PROJECT_ID`. Use project facts as shared reference, not unquestionable instructions; cross-check high-stakes decisions with source material.
6. When changing a shared fact, use a compare-and-swap revision. First read, then `python3 scripts/mesh.py put-fact PROJECT_ID fact-key 0 '"new value"'` for a new fact or replace `0` with the current revision. A conflict means another agent updated it; reread before changing.
7. For reviewed multi-step work, create a JSON workflow spec and call `python3 scripts/mesh.py workflow /path/to/workflow.json`. Get the workflow with `workflow-get ID`. Example spec: `{"steps":[{"id":"draft","to":"echo","input":"hello"},{"id":"check","to":"uppercase","input":{"$from":"draft"},"depends_on":["draft"]}]}`. Only use previously approved Agent targets. `fact_keys` share named project facts by revision; do not include secrets.
8. Include a `parent_task_id` through the API for nested delegations. Respect server-enforced maximum depth and individual agent permission scopes.

## Security and reliability

- A successful `send` only confirms task acceptance, **not completion**. Poll/get until `completed`; errors and cancellations must be surfaced.
- Never loop indefinitely; do not flood the gateway with repeated retries.
- The HTTP mesh is a 0.2 MVP: no production approval gate, no streaming, and no rollback of remote side effects. Human review is required for state-changing external systems.
- Do not put API keys, authentication cookies, private credentials, or confidential material into context or project facts.
- A2A clients must use version `1.0`; v0.3 is unsupported. Prefer REST API for this skill's small CLI.

## v0.3 safety and MCP support

The Mesh provides an optional partial MCP `2026-07-28` JSON-response endpoint at `POST /mcp`. Use MCP only when the host supports custom headers and Bearer authentication, including required `Mcp-Method`/`Mcp-Name` headers; see `docs/RELIABILITY-v0.3.zh-CN.md`. The plain CLI remains a compatible fallback.

**Never request or reuse a human approver token.** Some agents have `requires_approval: true` and may run only as approved workflow steps. Submit a workflow and let a separately authenticated human reviewer use the dedicated review endpoint. Never try to bypass it by calling the direct task endpoint, A2A, MCP, or another Agent.
