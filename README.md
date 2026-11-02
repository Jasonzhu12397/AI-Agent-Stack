# AI Agent Stack

An extensible, policy-governed AI Agent platform. The project is developed milestone by milestone, with a secure execution runtime and integration architecture.

## Current status

**M0 foundation — initial scaffold committed.** The current slice includes:
- FastAPI service with liveness and PostgreSQL readiness endpoints.
- Independent Worker process with graceful shutdown and database heartbeat.
- PostgreSQL 16 as the persistent data store; Redis 7 is auxiliary.
- Docker Compose development environment.
- Pytest and Ruff CI workflow.

The Worker does not yet claim or execute durable tasks. Agent authoring, workflows, approvals, MCP, Skills, and Kubernetes adapters are planned milestones, not implemented features.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

- API: http://localhost:8000
- OpenAPI: http://localhost:8000/docs
- Liveness: http://localhost:8000/health/live
- Readiness: http://localhost:8000/health/ready

Stop services:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml down
```

## Local checks

With Python 3.12:

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

## Architecture direction

- `apps/platform-api`: HTTP API and application boundary.
- `apps/worker`: independently deployable background process.
- `packages/`: planned contracts, domain, runtime, integrations, and observability modules.
- `deploy/`: local and future deployment assets.
- `docs/vibe-coding.md`: milestone plan and acceptance principles.

PostgreSQL is the only authoritative persistence layer. Redis must not be used as the sole store for runs, approvals, audit events, or other durable business state.

## Security baseline

Model output is untrusted structured intent, never authorization. Future tool execution must enforce schema validation, actor/tenant scope, allowlists, resource constraints, and policy decisions before any side effect. Credentials must be referenced through secret handles and must never be copied into prompts, traces, or logs.

No production infrastructure writes or external write integrations are enabled in this milestone.
