# Verification matrix

This document distinguishes implemented code from behavior that has actually been exercised. Status is not inferred from file existence.

| Area | Target | Evidence / current status |
|---|---|---|
| Python dependencies | Python 3.12 install | Verified by GitHub Actions run #8 |
| Ruff | `ruff check .` | Passed in run #8 |
| Python unit tests | `pytest` | Passed in run #8; see workflow run #8 |
| Frontend typecheck/build | `npm ci && npm run build` | Not yet in CI; unverified |
| Compose syntax | `docker compose config` | Unverified |
| Clean Compose startup | API, Worker, PostgreSQL, Redis, Web | Unverified |
| Alembic upgrade | Empty PostgreSQL -> head | Unverified |
| Alembic downgrade/upgrade | head -> base -> head | Unverified |
| API readiness | API + real PostgreSQL | Unverified outside CI; health unit test does not prove DB readiness |
| Worker | Process startup/shutdown | Heartbeat code exists; durable job execution is not implemented |
| FakeModel/FakeTool | Deterministic local contract tests | Pending |
| FakeMCP | Local fixture behavior | Pending; not real MCP protocol compatibility |
| Tenant isolation | API and database object authorization | Not implemented |
| Durable task queue | Atomic claim, lease, recovery, idempotency | Not implemented |
| External integrations | Vendor API contract and scopes | Not verified; no live credentials or calls |
| Kubernetes/CAPI/Metal3/Ironic | Target CRD/API compatibility | No target environment/version matrix supplied; not verified |
| Production readiness | Security, backup/restore, HA, SLO | Not implemented / not verified |

## Evidence policy

- Link each verified item to an immutable commit and CI run or a reproducible local command log.
- Mark tests as skipped when their dependencies are unavailable; never convert skipped to passed.
- Contract fixtures and simulations are labeled as simulations, not live integration tests.
- A later successful run does not erase earlier failures; record the fix commit and the new run.
