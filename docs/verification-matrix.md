# Verification matrix

Evidence is tied to immutable commits and CI runs. A file existing is not proof of behavior.

## Historical verification: FastAPI + React

[GitHub Actions run 41404656581](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41404656581), commit `71d3ae6684902befc8ca851ebe98705571af83c3`, passed the former FastAPI/React stack. This does not verify the Django/Vue migration.

## Latest verified commit

[GitHub Actions run 41420995224](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41420995224), commit `02d1ca8c3779ce9ac7bcd3096fec551f0544165a`, completed successfully across Python, Vue and Compose jobs. Pytest: 61 passed. Ruff, Django checks, PostgreSQL migrations, Vue production build and Compose API/Web/Worker smoke tests passed. This run includes the basic Workflow DAG backend, Workflow Studio UI, request validation and idempotency tests. Later documentation commits are being revalidated.

[Durable worker run 41409745762](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41409745762), commit `287fe2daab5470a7326f2b2f1320e60f9cc62a23`, passed with 36 tests.

[Session authentication run 41408491282](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41408491282), commit `7bc5cb4f33b0911f2648b5302e557c694cd0863e`, passed with 28 tests. [Membership administration run 41408139658](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41408139658) passed with 26 tests.

[Membership administration run 41408139658](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41408139658), commit `a5629956683bd5a246149b786e67c8a4b1bd1862`, passed with 26 tests before the authentication endpoints were added.

## Django + Vue migration

| Area | Target | Status |
|---|---|---|
| Backend | Django 5.1+ / Python 3.12 | Passed in run 41405916271 |
| REST API | Django REST Framework | Tenant self-service creation, project/member APIs, session auth and scoped asynchronous task create/list/detail/cancel APIs tested in runs 41408139658, 41408491282 and 41410163318 |
| ASGI | Django Channels / Daphne | HTTP ASGI entrypoint passed in Compose smoke test; WebSocket consumers not implemented |
| Production configuration | Reject default SECRET_KEY and DEBUG=true | Negative test passed in run 41406654505 |
| Database | PostgreSQL via dj-database-url | Migration drift check and PostgreSQL migrations passed in run 41407102851 |
| Health | Liveness and database readiness | Both endpoints passed in Compose smoke test, run 41407102851 |
| Worker | Separate Python process / durable PostgreSQL queue | Atomic `select_for_update(skip_locked)` claim, attempt counter, lease owner/token, lease expiry recovery, heartbeat renewal, bounded retries, cooperative cancellation and persisted terminal states. Demo, single-turn LLM Agent and basic Workflow Run handlers are connected; Workflow execution evidence is in run 41420838331 |
| Frontend | Vue 3 + TypeScript + Vite | Production build, session login, tenant/project workspace, task queue, fake model manager, Agent Builder/run console and Workflow Studio JSON editor/publish/run history passed in run 41420838331 |
| UI components | Element Plus | Installed and registered |
| State management | Pinia | Installed and registered |
| Routing | Vue Router | Dependency selected; route table not yet implemented |
| Compose | API, Worker, PostgreSQL, Redis and Vue | Build/start and API/Web/Worker smoke tests passed in run 41407102851 |

## Not yet implemented or verified

| Area | Target | Current status |
|---|---|---|
| Identity and authorization | Server-derived Principal, tenant/project RBAC | Partial: Django session login/logout/current-user, CSRF protection, tenant/project RBAC, membership administration, last-owner/last-project-admin protections and membership revocation tested. OIDC/OAuth2, invitations/email workflow, password reset, service identities and fine-grained resource/tool policies remain unimplemented. |
| Durable task queue | Atomic claim, lease, heartbeat, expiry recovery, idempotency | Implemented for safe demo tasks and text-only Agent Runs; tests cover idempotent enqueue, role authorization, completion, stale lease rejection, lease recovery, retryable failure path and cancellation. No external side-effect handlers or Workflow Engine integration yet |
| Agent runtime | Run snapshots, model routing, Tool Router, durable steps | Partial: published Agent versions and asynchronous single-turn LLM runs work. Multi-step tool calling, model routing/fallback, memory and tool policy integration remain unimplemented |
| Workflow engine | Versioned DAG, branches, recovery | Partial: immutable versions; start/LLM/condition/end nodes; DAG validation; branch skip; persisted node steps; idempotent run creation. No drag/drop canvas, loop/parallel/join, human approval, HTTP/Code nodes, scheduler/webhook triggers or fault-injection proof of process restart recovery |
| Policy and approval | Default deny, hash-bound one-time approval | Not implemented; high-risk external actions remain disabled |
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
