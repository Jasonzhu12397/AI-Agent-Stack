# AI Agent Stack

An extensible, policy-governed AI Agent platform with separate frontend and backend technology stacks.

## Current architecture

- **Frontend:** Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- **Backend:** Python 3.12, Django, Django REST Framework and Django Channels/ASGI.
- **Persistence:** PostgreSQL is the authoritative business datastore; Redis is auxiliary.
- **Execution:** independent Python Worker with PostgreSQL-backed task records, atomic claims, lease tokens/expiry, heartbeat renewal, bounded retries and cooperative cancellation. Safe demo handlers, single-turn LLM Agent runs and a basic persistent Workflow DAG runner are connected.
- **Object and vector storage:** S3-compatible storage and pgvector are planned integration points.
- **Deployment:** Docker Compose locally; Kubernetes and Helm for production.

The FastAPI and React implementation has been replaced. The latest verified workflow milestone passed full CI in [run 41420838331](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41420838331), commit `fb453e9d4afff95c220968274b2fe8daf85399fc`: Ruff, 60 pytest tests, Vue production build, PostgreSQL migrations, Compose build/start, API health checks, Web HTTP and Worker-running smoke test. A newer run is validating follow-up request-shape and UI fixes.

The P1-1 identity slice adds Tenant, TenantMembership, Project and ProjectMembership models. Tenant and project lists are membership-filtered; only tenant owners/admins can create projects, and the creator receives project-admin membership. Cross-tenant, tenant_id spoofing, role denial, revocation and pagination isolation are tested. Login UI/OIDC, membership invitations/administration and project role-specific update/delete permissions remain unimplemented.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

- Vue Studio: http://localhost:5173
- Django API: http://localhost:8000/api/v1/
- Django Admin: http://localhost:8000/admin/

Create the first local administrator in another terminal after the stack is running:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml exec api python apps/platform-api/manage.py createsuperuser
```

Use that account to sign in to the Vue Studio; tenant membership provisioning is still an administrative workflow.
- Task API: `/api/v1/tenants/{tenant_id}/projects/{project_id}/tasks/` (requires login and an `Idempotency-Key` header)
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

## Workflow milestone

Workflow definitions are tenant/project scoped JSON DAGs. The current engine supports `start`, `llm`, `condition` and `end` nodes, validates graph structure and model scope at publish time, stores immutable version snapshots, creates idempotent asynchronous runs and persists per-node status/output. The Vue Workflow Studio currently provides draft editing through a JSON editor, publish, run and execution history. It is not yet a drag-and-drop canvas.

Loops, parallel/join, human approval, HTTP/Code nodes, scheduler/webhook triggers, workflow event streaming and a general Tool Router remain future work. The current LLM node is a model call only; it does not yet invoke external plugins or MCP tools.

## Security baseline

Production rejects DEBUG mode and the development SECRET_KEY. DRF defaults to authenticated access; public endpoints must explicitly opt in. Model output, retrieved content, Skills, MCP responses and plugin data are untrusted. Authorization is enforced by server-side policy, never by model intent or client-supplied tenant identifiers.

No production infrastructure writes or external write integrations are enabled in this milestone.
