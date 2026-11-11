# Product roadmap

- **v0.1 (this bundle):** portable API; basic A2A 1.0 JSON-RPC; generic HTTP/Python agent adapters; persist tasks; ACL, audit, idempotency, hop limits; project knowledge with revision history; portable Agent Skill; tests.
- **v0.2:** full A2A SDK adapter with conformance tests; streaming SSE, files, multi-turn continuation, push callbacks and cancellation propagation; structured capability schema.
- **v0.3:** mTLS/OIDC, signed Agent Cards, human approvals, per-agent secrets, explicit trust policies, PostgreSQL + Redis leases, retries, quotas, multi-tenant isolation.
- **v0.4:** first-party framework-specific wrappers for OpenAI Agents SDK, LangGraph, CrewAI and other frameworks; MCP tools exposing mesh calls; CLI scaffolding.
- **v0.5:** coordinator policies, knowledge-base with versioned decisions and evidence, conflict coordination, budget limits, distributed tracing, automated quality gates.

**Architecture rule:** the protocol layer transports messages; the coordination layer decides *which* agent, *when*, *with what evidence*, and *under whose authorization*. Neither the wire protocol nor a model's claims substitute for authorization.
