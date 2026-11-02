# AI Agent Stack

An extensible, policy-governed AI Agent platform with separate frontend and backend technology stacks.

## Current architecture

- **Frontend:** Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- **Backend:** Python 3.12, Django, Django REST Framework and Django Channels/ASGI.
- **Persistence:** PostgreSQL is the authoritative business datastore; Redis is auxiliary.
- **Execution:** independent Python Worker; durable task claiming and recovery are a later milestone.
- **Object and vector storage:** S3-compatible storage and pgvector are planned integration points.
- **Deployment:** Docker Compose locally; Kubernetes and Helm for production.

The FastAPI and React implementation has been replaced. The Django/Vue platform passed full CI in [run 41406654505](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41406654505), commit `299106b111285503e6cc002d0109dd935f90c375`: Python 3.12 install, Ruff, Django checks, PostgreSQL migrations, 15 pytest tests, Vue production build, Compose build/start, API health checks, Web HTTP and Worker-running smoke test.

The first P1-1 identity slice adds Tenant, TenantMembership, Project and ProjectMembership models plus authenticated, membership-filtered tenant/project read APIs. It includes cross-tenant, spoofed tenant_id, revocation and pagination isolation tests. Tenant/project creation, invitations, login UI/OIDC and role-specific write permissions are not yet implemented.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

- Vue Studio: http://localhost:5173
- Django API: http://localhost:8000/api/v1/
- Django Admin: http://localhost:8000/admin/
- Liveness: http://localhost:8000/health/live
- Readiness: http://localhost:8000/health/ready

Stop services:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml down
```

## Local checks

Python 3.12:

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
python apps/platform-api/manage.py check
```

Frontend:

```bash
cd apps/web-studio
npm install
npm run build
```

## Repository layout

- `apps/platform-api/platform_api`: Django project configuration and ASGI entrypoint.
- `apps/platform-api/core`: shared HTTP endpoints and core Django app.
- `apps/worker`: independently deployable Python Worker.
- `apps/web-studio`: Vue 3 operator workspace.
- `packages/`: planned contracts, domain, runtime, integrations and observability modules.
- `deploy/`: local and future deployment assets.
- `docs/`: architecture decisions, threat model, verification matrix and delivery plan.
- `tests/`: backend contract and integration tests.

## Security baseline

Production rejects DEBUG mode and the development SECRET_KEY. DRF defaults to authenticated access; public endpoints must explicitly opt in. Model output, retrieved content, Skills, MCP responses and plugin data are untrusted. Authorization is enforced by server-side policy, never by model intent or client-supplied tenant identifiers.

No production infrastructure writes or external write integrations are enabled in this milestone.
