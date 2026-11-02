# Verification matrix

Evidence is tied to immutable commits and CI runs. A file existing is not proof of behavior.

## Historical verification: FastAPI + React

[GitHub Actions run 41404656581](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41404656581), commit `71d3ae6684902befc8ca851ebe98705571af83c3`, passed the former FastAPI/React stack. This does not verify the Django/Vue migration.

## Django + Vue migration

| Area | Target | Status |
|---|---|---|
| Backend | Django 5.1+ / Python 3.12 | Passed in run 41405916271 |
| REST API | Django REST Framework | API root and health endpoints passed in run 41405916271 |
| ASGI | Django Channels / Daphne | HTTP ASGI entrypoint passed in Compose smoke test; WebSocket consumers not implemented |
| Production configuration | Reject default SECRET_KEY and DEBUG=true | Negative test passed in run 41405916271 |
| Database | PostgreSQL via dj-database-url | PostgreSQL migrations applied successfully in run 41405916271 |
| Health | Liveness and database readiness | Both endpoints passed in Compose smoke test, run 41405916271 |
| Worker | Separate Python process / psycopg heartbeat | Implemented; not a durable task queue |
| Frontend | Vue 3 + TypeScript + Vite | Production build passed in run 41405916271 |
| UI components | Element Plus | Installed and registered |
| State management | Pinia | Installed and registered |
| Routing | Vue Router | Dependency selected; route table not yet implemented |
| Compose | API, Worker, PostgreSQL, Redis and Vue | Build/start and API/Web/Worker smoke tests passed in run 41405916271 |

## Not yet implemented or verified

| Area | Target | Current status |
|---|---|---|
| Identity and authorization | Server-derived Principal, tenant/project RBAC | Not implemented |
| Durable task queue | Atomic claim, lease, heartbeat, expiry recovery, idempotency | Not implemented; Worker heartbeat only |
| Agent runtime | Run snapshots, model routing, Tool Router, durable steps | Not implemented |
| Workflow engine | Versioned DAG, branches, recovery | Not implemented |
| Policy and approval | Default deny, hash-bound one-time approval | Not implemented |
| Real MCP | SDK, transport, negotiation and protocol tests | Not implemented; FakeMCP is an in-process fixture |
| Plugin isolation / SSRF | Sandboxed executor and network egress controls | Not implemented |
| RAG / Memory | ACL-filtered ingestion, retrieval and deletion | Not implemented |
| Vendor connectors | GitHub/Jira/Graph/Maps/Weather and others | Not implemented / no live API verification |
| Kubernetes/CAPI/Metal3/Ironic | Target CRD/API versions and test-cluster operations | Not verified |
| Production readiness | OIDC, secret manager, backup/restore, HA, SLO and security scans | Not implemented / not verified |

## Evidence policy

- Link verified items to immutable commits and CI runs or reproducible local command output.
- Tests that are skipped or absent remain unverified.
- Simulations are labeled as simulations, not live integrations.
- A successful migration CI run is required before this stack is called validated.
