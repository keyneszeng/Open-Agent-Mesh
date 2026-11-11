# Contributing to Open Agent Mesh

Welcome. The initial code is a research MVP, not a production-certified interoperable A2A platform.

## Workflow

1. Open or select a GitHub Issue. Include motivation, scope, threat assumptions and acceptance criteria.
2. Work on a feature branch where possible; keep patches small and reviewable.
3. Add tests: unit, failure cases and at least one end-to-end test for protocol or orchestration changes.
4. Update documentation, compatibility and security caveats plus CHANGELOG.md.
5. Submit a PR with reproducible commands and link to the issue.

## Local checks

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python tests/smoke_network.py
```

The smoke test starts local listeners and may require Node.js for the JavaScript example.

## Safety and scope

Do not check in real credentials, tokens, private chats, customer data, or third-party proprietary source. Do not expose this gateway publicly with demo credentials. Treat remote agent content as untrusted. New external network integrations need SSRF/egress and credential-leakage review.

## Protocol claims

Identify the exact A2A method/version tested and the SDK implementation used. A successful round-trip with another instance of Mesh is not sufficient to claim independent A2A 1.0 conformance.

## Licensing

The original code uses the MIT License; contributor submissions must be compatible and include any necessary attributions.