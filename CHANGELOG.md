# Changelog

All important implementation and research updates are recorded here.

## [Unreleased]

- Added permanent project charter, governance instructions, contribution/PR template and commercial research hypotheses.
- Added dated research notes connecting Cursor-inspired Agent collaboration with the open A2A/MCP/Skills architecture.
- Created priority research Issues #1–#6 for interoperability, security, coordination, adapters, knowledge and business validation.
- Extended GitHub Actions verification to include cross-process networking smoke tests.
- Repository is the canonical development baseline. Future changes should be committed with tests and documentation.

## [0.1.0] — 2026-11-11

- **Source published to GitHub**: 26 original source, test, config and documentation files imported to main in commit `1674bdd28b085be52deb8484133229854609eb8f`.
- Basic A2A 1.0 JSON-RPC subset and HTTP agent adapters.
- Task storage, events, scoped bearer authorization, idempotency and hop limits.
- Versioned project knowledge, example coordinator and portable Agent Skill.
- Python and Node examples, network smoke test.
- Passed 24 local core tests and cross-process integration smoke tests.
- Verified GitHub Actions original 24-test workflow succeeded.

**Limitations:** v0.1 is an MVP and has not passed independent full A2A conformance or a production security audit.