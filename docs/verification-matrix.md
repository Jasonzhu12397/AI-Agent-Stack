# Verification matrix

Evidence is tied to GitHub Actions runs and immutable commits. A file existing is not proof of behavior.

## Verified in CI

Latest full-stack verification: [GitHub Actions run 41404365864](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41404365864), commit `a76ea2bbb32fca327c36b8e61b48080c8d300db9`, conclusion `success`.

| Area | Target | Evidence / status |
|---|---|---|
| Python dependencies | Python 3.12 install | Passed in run 41404365864 |
| Ruff | `ruff check .` | Passed in run 41404365864 |
| Python unit tests | `pytest` | Passed in run 41404365864; includes FakeModel/FakeTool/FakeMCP fixture contract tests |
| Frontend | npm dependency install + TypeScript/Vite build | Passed in run 41404365864 |
| Compose syntax | `docker compose config --quiet` | Passed in run 41404365864 |
| Clean Compose startup | Build/start API, Worker, PostgreSQL, Redis, Web | Passed in run 41404365864 |
| API liveness | `GET /health/live` | Passed in Compose smoke test |
| API readiness | `GET /health/ready` with PostgreSQL | Passed in Compose smoke test |
| Web Studio HTTP | `GET http://localhost:5173/` | Passed in Compose smoke test |
| Worker process | Worker container remains running | Passed in Compose smoke test |
| Alembic migration | PostgreSQL 16: upgrade head -> downgrade base -> upgrade head | Passed in run 41404365864 |
| FakeModel | Structured intent validation / unexpected-field rejection | Passed in pytest |
| FakeTool | Deterministic echo / schema rejection | Passed in pytest |
| FakeMCP fixture | In-process register/list/call / unknown and duplicate tool rejection | Passed in pytest; this is not real MCP protocol validation |

## Not yet verified or not implemented

| Area | Target | Current status |
|---|---|---|
| Full migration coverage | Schema migration with all future entities and constraints | Not implemented; current migration creates only Tenant |
| API database integration | Readiness and data operations under DB failure/recovery | Readiness exercised in Compose; failure/recovery cases not yet covered |
| Authentication and authorization | Server-derived Principal, tenant/project object authorization | Not implemented |
| Durable task queue | Atomic claim, lease, heartbeat, expiry recovery, idempotency | Not implemented; Worker currently performs a DB heartbeat only |
| Agent runtime | Run snapshots, model routing, Tool Router, durable steps | Not implemented |
| Workflow engine | Versioned DAG, branches, recovery | Not implemented |
| Policy and approval | Default deny, hash-bound one-time approval | Not implemented |
| Real MCP | Selected SDK, transport, negotiation, local protocol server tests | Not implemented; FakeMCP is only an in-process fixture |
| Plugin isolation / SSRF | Sandboxed executor and network egress controls | Not implemented |
| RAG / Memory | ACL-filtered ingestion and retrieval/deletion | Not implemented |
| Vendor connectors | GitHub/Jira/Graph/Maps/Weather and others | Not implemented / no live API verification |
| Kubernetes/CAPI/Metal3/Ironic | Target CRD/API versions and test-cluster operations | Not verified; no target environment/version matrix supplied |
| Production readiness | OIDC, secret manager, backup/restore, HA, SLO, security scans | Not implemented / not verified |

## Evidence policy

- Link each verified item to an immutable commit and CI run or reproducible local command output.
- Tests that are skipped or not present remain unverified.
- Contract fixtures and simulations are labeled as simulations, not live integrations.
- A later successful run does not erase earlier failures; record the fix commit and new run.
