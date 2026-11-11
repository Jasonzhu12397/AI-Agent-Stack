# AI Agent Stack

An extensible, policy-governed AI Agent platform with separate frontend and backend technology stacks.

## Product direction

The long-term infrastructure product scope is documented in [Omni-Inspired Infrastructure Platform Roadmap](docs/omni-parity-roadmap.md). Omni is a capability benchmark; this project uses CAPI + Metal3 + Ironic and the dual-Agent EPH/Target lifecycle rather than copying Talos-specific implementation.

## EPH RAM Live ISO

The first EPH Live ISO builder and per-node NoCloud seed generator are under [images/eph-live](images/eph-live/README.md). The ISO boots Debian Live with toram, initializes a single-node Kubernetes cluster, and deploys the EPH Agent as a Kubernetes Deployment. This is an initial implementation; QEMU boot validation and hardware qualification remain release gates.

## Current architecture

- **Frontend:** Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- **Backend:** Python 3.12, Django, Django REST Framework and Django Channels/ASGI.
- **Persistence:** PostgreSQL is the authoritative business datastore; Redis is auxiliary.
- **Execution:** independent Python Worker with PostgreSQL-backed task records, atomic claims, lease tokens/expiry, heartbeat renewal, bounded retries and cooperative cancellation. Safe demo handlers, single-turn LLM Agent runs and a basic persistent Workflow DAG runner are connected.
- **Integration Hub:** tenant-scoped versioned connector metadata, tool schemas, admin review, project installation, explicit permission grants and credential-reference records. The Worker supports normalized HTTP/OpenAPI contracts through an HTTPS host allowlist, bounded requests, connection health checks and a pluggable credential resolver; raw OpenAPI import, OAuth refresh, MCP transport and provider-specific connectors remain in progress.
- **Infrastructure control:** a central remote control plane can enroll an EPH Node Agent and a Target Cluster Agent separately. A persisted Cluster Delivery Plan orchestrates EPH preflight → CAPI/Metal3 plan validation, stores the plan hash and missing prerequisites, and exposes status/cancellation in Vue. Commands are typed, tenant/project scoped, persisted and lease-protected.
- **Node agents:** the Python runner supports EPH preflight, typed Metal3/CAPI cluster-plan validation, dynamic CAPI/Metal3 API discovery and read-only Kubernetes inspection from an in-cluster ServiceAccount. It does not execute arbitrary shell or apply CAPI/Metal3 manifests yet.
- **Object and vector storage:** S3-compatible storage and pgvector are planned integration points.
- **Deployment:** Docker Compose is limited to the local Cloud Control Plane development stack and the production cloud Control Plane profile. EPH and Target Agents run only as Kubernetes Pods from dedicated Deployments; the EPH Agent runs inside the temporary single-node EPH Kubernetes cluster. The target network-inventory publisher runs as a DaemonSet.

The FastAPI and React implementation has been replaced. The Vue UI follows the layout and interaction patterns in [AI-Stack-Agent-GUI](https://github.com/Jasonzhu12397/AI-Stack-Agent-GUI), ported from its React implementation rather than embedding React into this Vue application. The navigation includes Dashboard, Model Manager, Agent Studio, Workflow DAG, Integration Hub, Knowledge Bases, Metal3 Fleet, Governance, Audit & Activity, Organization and Docs & Matrix. The previous verified workflow milestone passed full CI in [run 41421574449](https://github.com/Jasonzhu12397/AI-Agent-Stack/actions/runs/41421574449), commit `56d29c006031fa9363e79c88f2c365aa1649ebf2`; this branch's dual-agent work is awaiting its own PR CI.

The P1-1 identity slice adds Tenant, TenantMembership, Project and ProjectMembership models. Tenant and project lists are membership-filtered; only tenant owners/admins can create projects, and the creator receives project-admin membership. Cross-tenant, tenant_id spoofing, role denial, revocation and pagination isolation are tested. Login UI/OIDC, membership invitations/administration and project role-specific update/delete permissions remain unimplemented.

## Production cloud deployment

See [Cloud Control Plane deployment](deploy/cloud-control-plane/README.md) for the production Compose profile and [dual-cluster Agent deployment](deploy/agents/README.md) for EPH and Target Agents.

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

### Deploy the EPH and Target Agents as Kubernetes Pods

The EPH Agent is **always a Kubernetes Pod inside the temporary single-node Kubernetes cluster running on the EPH bare-metal host**. This is the same Kubernetes-native operating model used by Metal3/Ironic controllers: Kubernetes Deployments, ServiceAccounts, RBAC, Secrets, probes, resource limits and Pod lifecycle management. The Target Agent is also deployed as a Pod inside the target Kubernetes cluster. Neither Agent is launched by Docker Compose or as a standalone host process.

1. Sign in to the Studio and create/select a tenant and project. The enrolling user must be a tenant owner or tenant administrator; project-only membership is not sufficient.
2. Open **Infrastructure Agents** and enroll the EPH and Target agents separately. Choose **EPH Node Agent** (`eph_node`) for the temporary EPH cluster and **Target Cluster Agent** (`target_cluster`) for the target cluster.
3. The Studio creates the AgentNode UUID and generates the one-time token server-side with Python `secrets.token_urlsafe(36)`. The API returns the token as `enrollment_token` exactly once; the database stores only its SHA-256 hash. Copy each node UUID and token into your secret manager. If a token is lost, use **Rotate credential**; never reuse EPH credentials for Target.
4. Prepare the Kubernetes Secrets, mTLS certificates and (for EPH) the source/target CAPI kubeconfigs as described in [Kubernetes Agent deployment](deploy/agents/README.md). Do not place credentials in Compose environment files or Git.
5. Build and push the digest-pinned Node Agent image, then deploy it into the two independent Kubernetes contexts:

```bash
EPH_CONTEXT=<eph-single-node-k8s-context> \
TARGET_CONTEXT=<target-cluster-k8s-context> \
AGENT_IMAGE=<registry>/ai-agent-node-agent@sha256:<64-hex-digest> \
bash deploy/agents/deploy.sh
```

The helper performs server-side dry-run validation, applies the EPH Deployment to the EPH single-node cluster, applies the Target Deployment and network-inventory DaemonSet to the target cluster, then waits for all workloads to become Ready. The EPH Pod is scheduled by the EPH Kubernetes scheduler and talks to the local Kubernetes API using its dedicated ServiceAccount. It does not run outside Kubernetes.

6. In the Studio, dispatch `node.health` to both enrolled Agents. Use the **Metal3 / CAPI cluster plan** workflow on the EPH Agent to run the read-only preflight and plan checks.

Docker Compose is reserved for the local Cloud Control Plane development stack (API, Cloud Agent worker, database, cache, vector database and web UI). It must never start, supervise or substitute for the EPH or Target Agent Pods.

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

The cluster plan workflow is created through `POST /api/v1/tenants/{tenant_id}/projects/{project_id}/cluster-deployments/`. It persists the deployment, queues `host.preflight`, automatically queues `cluster.plan` after a successful preflight, and exposes the resulting plan via `GET /api/v1/cluster-deployments/{id}/`. The workflow stops at `PLAN_READY`; it does not install Kubernetes.

The EPH runner currently supports:
- `node.health`, `host.preflight`, `cluster.plan` (validates cluster identity, odd control-plane quorum, provider-version references, network/image inputs, BMH references and credential references; dry-run only);
- `cluster.provider.compatibility` (discovers CAPM3/BMO/Ironic/IrSO image tags from live Kubernetes Deployments, compares them with the Metal3 community support and periodic CI matrices, and cross-checks live Kubernetes/CAPI API versions on both agent roles; read-only and never authorizes provisioning);
- `cluster.health`, `cluster.observe`, `cluster.diagnose`, `cluster.postflight`, `k8s.resource.read` (read-only in-cluster Kubernetes API with output summaries and redaction);
- `capi.cluster.observe` (discovers installed CAPI API version and reads Cluster/Machine/MachineDeployment summaries);
- `metal3.host.inspect` (discovers Metal3 API version and reads BareMetalHost summaries without returning BMC credential fields).
- `cluster.network.inspect` (read-only discovery of Calico/Multus/OVS/SR-IOV indicators, NetworkAttachmentDefinitions, node SR-IOV/hugepage resources, optional host NIC/PCI PF/VF/IOMMU/NUMA inventory through read-only sysfs, Agent Pod routes and explicitly scoped workload Pod Multus attachments); cluster plans optionally validate a versioned Control Plane/Data Plane network profile with Data Plane Pod traffic as the default.
- `cluster.capi.pivot.dry-run` (EPH-only; invokes `clusterctl move --dry-run` using source and target kubeconfigs mounted from a Kubernetes Secret; reports namespace-wide scope and never mutates resources).

Kubernetes Deployment/RBAC templates for running both agent roles as pods are in `deploy/agents/`.

The Target Cluster Agent must be installed on a target control-plane node immediately after the first Control Plane is Ready, before Data Plane workers are provisioned. Its Deployment uses a control-plane node selector and tolerates the standard control-plane NoSchedule taint so it can run when no workers exist. It resumes the same logical deployment/task from the central Supervisor using a separate target-cluster identity; this is a new Pod/process, not a live memory transfer. If CAPI controllers and Cluster API objects were hosted by the EPH bootstrap cluster, pivot them into the target cluster and verify reconciliation before stopping EPH or creating workers. The Target Agent then coordinates remaining Control Plane replicas and Data Plane workers. Production node identity should move to mTLS/short-lived workload identity; the current one-time bearer token is the initial development enrollment mechanism.

**Not yet enabled:** actual CAPI Cluster/MachineDeployment creation, Metal3 BareMetalHost writes, Ironic/BMC operations, target-agent installation, arbitrary manifest apply and day-2 mutation. These require a verified management/target Kubernetes + CAPI + Metal3 + Ironic + IPA compatibility matrix, scoped credentials and an execution adapter. A command marked approved is not proof that provisioning occurred.

## Workflow milestone

Workflow definitions are tenant/project scoped JSON DAGs. The current engine supports `start`, `llm`, `condition` and `end` nodes, validates graph structure and model scope at publish time, stores immutable version snapshots, creates idempotent asynchronous runs and persists per-node status/output. The Vue Workflow Studio currently provides draft editing through a JSON editor, publish, run and execution history. It is not yet a drag-and-drop canvas.

Loops, parallel/join, human approval, HTTP/Code nodes, scheduler/webhook triggers, workflow event streaming and a general Tool Router remain future work. The current LLM node is a model call only; it does not yet invoke external plugins or MCP tools.

## Security baseline

Production rejects DEBUG mode and the development SECRET_KEY. DRF defaults to authenticated access; public endpoints must explicitly opt in. Model output, retrieved content, Skills, MCP responses and plugin data are untrusted. Authorization is enforced by server-side policy, never by model intent or client-supplied tenant identifiers.

No production infrastructure writes or external write integrations are enabled in this milestone.
