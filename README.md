# AI Agent Stack

An extensible, policy-governed AI Agent platform with separate frontend and backend technology stacks.

## Current architecture

- **Frontend:** Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- **Backend:** Python 3.12, Django, Django REST Framework and Django Channels/ASGI.
- **Persistence:** PostgreSQL is the authoritative business datastore; Redis is auxiliary.
- **Execution:** independent Python Worker with PostgreSQL-backed task records, atomic claims, lease tokens/expiry, heartbeat renewal, bounded retries and cooperative cancellation. Safe demo handlers, single-turn LLM Agent runs and a basic persistent Workflow DAG runner are connected.
- **Infrastructure control:** a central remote control plane can enroll an EPH Node Agent and a Target Cluster Agent separately. Commands are typed, tenant/project scoped, persisted, lease-protected and approval-hash bound for provisioning/bootstrap operations.
- **Node agents:** the Python runner supports EPH preflight, typed Metal3/CAPI cluster-plan validation, dynamic CAPI/Metal3 API discovery and read-only Kubernetes inspection from an in-cluster ServiceAccount. It does not execute arbitrary shell or apply CAPI/Metal3 manifests yet.
- **Object and vector storage:** S3-compatible storage and pgvector are planned integration points.
- **Deployment:** Docker Compose locally; Kubernetes and Helm for production.

The FastAPI and React implementation has been replaced. The Vue UI follows the layout and interaction patterns in [AI-Stack-Agent-GUI](https://github.com/Jasonzhu12397/AI-Stack-Agent-GUI), ported from its React implementation rather than embedding React into this Vue application. The previous verified workflow milestone passed full CI in [run 41421574449](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41421574449), commit `56d29c006031fa9363e79c88f2c365aa1649ebf2`; this branch's dual-agent work is awaiting its own PR CI.

The P1-1 identity slice adds Tenant, TenantMembership, Project and ProjectMembership models. Tenant and project lists are membership-filtered; only tenant owners/admins can create projects, and the creator receives project-admin membership. Cross-tenant, tenant_id spoofing, role denial, revocation and pagination isolation are tested. Login UI/OIDC, membership invitations/administration and project role-specific update/delete permissions remain unimplemented.

## Quick start

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

- Vue Studio: http://localhost:5173
- Infrastructure Agents: open the Infrastructure Agents tab after signing in.
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

### Run the two local node-agent processes

1. Sign in to the Studio and create/select a tenant and project.
2. Open **Infrastructure Agents** and enroll one `EPH Node Agent` and one `Target Cluster Agent`.
3. Copy each one-time token and node UUID immediately. The token is never returned again.
4. Add them to the ignored local `.env` file:

```dotenv
EPH_AGENT_NODE_ID=<eph-node-uuid>
EPH_AGENT_TOKEN=<eph-one-time-token>
TARGET_AGENT_NODE_ID=<target-node-uuid>
TARGET_AGENT_TOKEN=<target-one-time-token>
```

5. Start the two optional agent processes:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml --profile node-agents up --build -d eph-node-agent target-cluster-agent
docker compose --env-file .env -f deploy/docker-compose.yml logs -f eph-node-agent target-cluster-agent
```

6. In the Studio, dispatch `node.health` to both agents and `host.preflight` / `cluster.plan` to the EPH agent.

The local Compose target agent runs outside a Kubernetes cluster, so it can report node health but Kubernetes API commands require the agent to be deployed inside the target cluster with a dedicated ServiceAccount and CA/token mounts. The EPH Compose process reports its container environment, not the physical host. Do not use this local profile as a production deployment.

Stop services:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml --profile node-agents down
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

## Dual-agent infrastructure control

See [Dual-Agent Infrastructure Architecture](docs/dual-agent-infrastructure-architecture.md) and [ADR 0003](docs/adr/0003-remote-dual-agent-control-plane.md).

The control plane enrolls an `eph_node` and a `target_cluster` agent independently. Node enrollment returns a one-time token; only its SHA-256 hash is stored. The agent initiates outbound HTTPS heartbeats and command polls. PostgreSQL stores command state, idempotency key, attempts, lease token/expiry, result and error. Provisioning and target-agent bootstrap commands wait for a different tenant administrator to approve the exact plan hash.

The EPH runner currently supports:
- `node.health`, `host.preflight`, `cluster.plan` (validates cluster identity, odd control-plane quorum, provider-version references, network/image inputs, BMH references and credential references; dry-run only);
- `cluster.health`, `cluster.observe`, `cluster.diagnose`, `cluster.postflight`, `k8s.resource.read` (read-only in-cluster Kubernetes API with output summaries and redaction);
- `capi.cluster.observe` (discovers installed CAPI API version and reads Cluster/Machine/MachineDeployment summaries);
- `metal3.host.inspect` (discovers Metal3 API version and reads BareMetalHost summaries without returning BMC credential fields).

Kubernetes Deployment/RBAC templates for running both agent roles as pods are in `deploy/agents/`.

The Target Cluster runner supports node health and read-only Kubernetes inspection after it is installed inside the target cluster. It is not needed to bootstrap the cluster. Production node identity should move to mTLS/short-lived workload identity; the current one-time bearer token is the initial development enrollment mechanism.

**Not yet enabled:** actual CAPI Cluster/MachineDeployment creation, Metal3 BareMetalHost writes, Ironic/BMC operations, target-agent installation, arbitrary manifest apply and day-2 mutation. These require a verified management/target Kubernetes + CAPI + Metal3 + Ironic + IPA compatibility matrix, scoped credentials and an execution adapter. A command marked approved is not proof that provisioning occurred.

## Workflow milestone

Workflow definitions are tenant/project scoped JSON DAGs. The current engine supports `start`, `llm`, `condition` and `end` nodes, validates graph structure and model scope at publish time, stores immutable version snapshots, creates idempotent asynchronous runs and persists per-node status/output. The Vue Workflow Studio currently provides draft editing through a JSON editor, publish, run and execution history. It is not yet a drag-and-drop canvas.

Loops, parallel/join, human approval, HTTP/Code nodes, scheduler/webhook triggers, workflow event streaming and a general Tool Router remain future work. The current LLM node is a model call only; it does not yet invoke external plugins or MCP tools.

## Security baseline

Production rejects DEBUG mode and the development SECRET_KEY. DRF defaults to authenticated access; public endpoints must explicitly opt in. Model output, retrieved content, Skills, MCP responses and plugin data are untrusted. Authorization is enforced by server-side policy, never by model intent or client-supplied tenant identifiers.

No production infrastructure writes or external write integrations are enabled in this milestone.
