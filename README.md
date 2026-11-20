# AI Agent Stack

A **multi-tenant, multi-Agent AI SaaS platform** for building, publishing, orchestrating and governing AI Agents — with a built-in three-Agent infrastructure control plane for deploying real bare-metal Kubernetes clusters.

AI Agent Stack is not a single chatbot or a collection of disconnected Agent demos. It combines a SaaS application runtime for AI Agents with an infrastructure runtime that can turn approved physical servers into a working Kubernetes control plane and data plane.

## Platform overview

| SaaS capability | What the platform provides | Current state |
| --- | --- | --- |
| Agent management | Agent definitions, versions, publishing and run records | Code baseline |
| Multi-Agent teams | Team definitions, specialists, coordinator synthesis, parallel fan-out and router dispatch | Code baseline; sequential, coordinator, parallel and router strategies |
| Multi-step orchestration | Workflow nodes: `start`, `llm`, `agent`, `condition`, `knowledge_search`, `tool`, `approval`, `end` | Parallel any/all joins and single-approver approval resume implemented; bounded loop pending |
| Model access | OpenAI-compatible model gateway, chat and embeddings | Code baseline; multi-provider routing/fallback pending |
| Tools and integrations | HTTP, OpenAPI and MCP streamable-HTTP tools, review and permission grants | Code baseline; OAuth and stdio MCP pending |
| Knowledge and RAG | Knowledge bases, Qdrant collections versioned per embedding model and dimension | Code baseline; real Qdrant chain pending validation |
| Asynchronous execution | PostgreSQL-backed Worker claims, leases, heartbeats, retries and cancellation | Code baseline |
| SaaS tenancy | Tenants, projects, `admin/editor/viewer` roles and server-side isolation | Code baseline |
| Usage governance | Per-tenant token budget reservation, settlement and overspend protection | Pending validation under real concurrency |
| Audit | Tenant and global chained-hash audit records with verification APIs | Code baseline |
| Infrastructure delivery | Cloud Control, EPH and Target Agents for CAPI/Metal3/Ironic bare metal | Pending real hardware validation |
| Control workspace | Installation-scoped projects with versioned skills, referenced plugins, tasks, DAG workflows and leased work claimed by Agents | Code baseline; control plane stores manifests, Agents execute |
| Software engineering agent | Static project understanding, approved multi-file patches, review, static and measured (cProfile) performance, tests, debugging, git, security, OpenAPI, docs, deployment drafts, schema-validated model planning and concurrent role collaboration | Code baseline; provider calls require explicit environment configuration |

External subscription billing and plan management are **not implemented**. The platform currently provides the multi-tenant and usage-governance foundations above; it does not claim to be a production-complete commercial SaaS.

## Multi-Agent architecture

AI Agent Stack has two cooperating multi-Agent runtime families.

### 1. Application multi-Agent runtime

- **Platform API** (`apps/platform-api`): Django and Django REST Framework API for authentication, tenants, projects, Agents, Workflows, models, knowledge, connectors, deployments, approvals and audit.
- **Web Studio** (`apps/web-studio`): Vue 3 operator workspace covering Agent Studio, Workflow Studio, Model Manager, Integration Hub, Knowledge Base, Infrastructure Agents, Metal3 Fleet, Governance, Audit and Organization.
- **Platform Worker** (`apps/worker`): independent asynchronous executor for Agent, Workflow and Agent Team tasks. It owns atomic task claims, lease tokens, heartbeat renewal, bounded retries and cooperative cancellation.
- **Agent Teams**: a project-scoped team publishes a versioned roster and one strategy: `sequential`, `coordinator`, `parallel` fan-out or `router` dispatch. Publishing materializes an immutable Workflow version made of `agent` and routing nodes, so every team run reuses the same lease, token-budget, retry and audit machinery as ordinary Workflows. Runs are idempotent by `Idempotency-Key` and expose per-member step records. See `docs/agent-teams.md`; members with plugin tools are rejected until per-member tool approvals exist.
- **Model gateway**: OpenAI-compatible chat and embeddings endpoints with retry classification. `fake` exists only for tests.
- **Integration Hub**: connector catalog, project installation, explicit grants, credential references, HTTP/OpenAPI tools and MCP tool discovery/import. Newly discovered MCP tools remain disabled until reviewed.
- **Knowledge/RAG**: document ingestion, embeddings and Qdrant retrieval. Collections are versioned by embedding model and dimension; `reindex_knowledge` supports dry-run and targeted re-embedding.

Platform Agents and Workflows execute in the Platform Worker. They are separate from the three infrastructure Agents that operate clusters and physical servers.

### 2. Infrastructure multi-Agent control system

0. **Software engineering agent** — a CLI/library in `apps/dev_agent` that reads a repository, proposes and applies approved patches, runs tests, analyses crashes and prepares git/security/docs/deployment artefacts; see `docs/dev-agent.md`. Both EPH and Target Pods run it as a default `dev-agent-worker` sidecar over a shared `software-workspace` PVC, and it claims only `kind: "software"` work.
1. **Cloud Control Agent** — runs in the cloud management environment. Owns durable command state, approvals, scheduling, dispatch, status aggregation and failure coordination. It also hosts installation-scoped project workspaces (projects, versioned skills, referenced plugins and leased work items) and single-approver patch approvals bound to a work item and SHA-256 payload digest; approvals never gate autonomous EPH/Target deployment work. See `docs/cloud-control-workspace.md`.
2. **EPH Agent** — runs in the temporary EPH Live OS / single-node Kubernetes cluster. Renders bootstrap configuration, applies CAPI/BMO/Ironic/CAPM3, creates BMH and target-CAPI resources, and records source-side identity evidence.
3. **Target Agent** — runs as three replicas on the target Control Plane with Kubernetes Lease leader election. Owns Target takeover, CAPI Pivot (`clusterctl move`), BMH migration, EPH cleanup, worker provisioning, NTP attestation and data-plane readiness.

There is no per-Worker Agent and no Network Inventory Agent DaemonSet. Worker facts come from Kubernetes, CAPI, Metal3 and approved infrastructure APIs.

## End-to-end architecture

```text
Users / Operators
       |
       v
Vue Web Studio  --->  Platform API (tenants, Agents, Workflows, models, tools, audit)
                              |
                              +--> PostgreSQL
                              |
                              +--> Platform Worker
                                     |--> Model Gateway
                                     |--> HTTP / OpenAPI / MCP tools
                                     |--> Knowledge / RAG / Qdrant
                                     |--> Agent and Workflow runs

Cloud Control Agent
       |
       v
EPH Agent (Debian 12 Live OS, single-node Kubernetes)
       |
       +--> CAPI + CAPM3 + BMO + Ironic + IPAM
       |
       v
Target Control Plane (Ubuntu 24.04 LTS by default)
       |
       v
Target Agent ---> CAPI Pivot / BMH migration / EPH cleanup
       |
       v
Target Workers ---> Calico eBPF data plane / CNI / SR-IOV
```

## Bare-metal delivery lifecycle

`eph_single_node_ready -> control_stack_ready -> bmh_registered -> bmh_ready -> target_control_plane_applied -> target_control_plane_ready -> target_agent_ready -> capi_pivot_verified -> bmh_registry_migrated -> eph_cleanup_verified -> worker_provisioning -> workers_ready -> network_ready -> completed`

- The EPH preflight reasons about the discovered fleet, records every control-plane/worker assignment with its justification, and derives the BMH host plan plus CAPI cluster spec. Those planning artifacts are allow-listed across checkpoints so later phases do not re-infer roles from a truncated list.
- Cloud Control Agent automatically plans and queues the next command after every successful result whose checkpoint has advanced; retries are deduplicated by phase-scoped idempotency keys.
- A one-minute `cloud-control-reconcile` CronJob replays interrupted auto-advance decisions, starts deployments whose EPH Agent registered late, and never races a queued or leased command.
- Target Control Plane masters become ready before Target Workers are provisioned.
- Target Agent restores its durable checkpoint after CAPI Pivot.
- BMH migration requires exact source/target equality of namespace, name and stable hardware identity, plus verified Target Metal3 ownership.
- EPH cleanup is blocked until CAPI ownership, BMH registry, controller health and credential revocation are positively verified.
- Every external side effect requires idempotency, a durable checkpoint and postflight evidence. Missing or ambiguous evidence fails closed.
- Kubernetes runs with Calico eBPF and kube-proxy replacement; kube-proxy is not installed.
- Target Workers synchronize time only from the Target Control Plane's Ironic internal IP/VIP over UDP/123; external NTP pools and fallback are prohibited.
- Kubernetes Control Plane management uses ordinary NIC connectivity. Pod traffic uses a separate Data Plane, with Multus and SR-IOV/DPDK where required.

The complete lifecycle is implemented at code-baseline and mock-test level. Real Live ISO, BMC, IPA, CAPI Pivot, physical Worker and Calico eBPF end-to-end acceptance remain outstanding.

## Open-source operating systems and package repositories

The EPH Live OS uses **Debian 12**. Target control-plane and worker images default to **Ubuntu 24.04 LTS**. Both can be deployed at scale without a paid operating-system subscription or per-node license; Ubuntu Pro is not required. The default package selection uses open-source components only: Debian `main` and Ubuntu `main universe`, without Debian `contrib/non-free` or Ubuntu `restricted/multiverse`.

OS and Kubernetes repositories are required **only while building EPH/Target OS images on a connected build machine**. Pin and mirror or snapshot them with the image build inputs:

```text
# Debian 12 (EPH)
deb https://deb.debian.org/debian bookworm main
deb https://deb.debian.org/debian bookworm-updates main
deb https://security.debian.org/debian-security bookworm-security main

# Ubuntu 24.04 LTS (Target)
deb https://archive.ubuntu.com/ubuntu noble main universe
deb https://archive.ubuntu.com/ubuntu noble-updates main universe
deb https://security.ubuntu.com/ubuntu noble-security main universe

# Kubernetes v1.37 packages (image build only)
deb https://pkgs.k8s.io/core:/stable:/v1.37/deb/ /
```

The OS images must already contain compatible containerd, kubeadm, kubelet and kubectl binaries at Kubernetes **`v1.37.2`**. EPH cloud-init and Target CAPI verify and enable them; they never execute online `apt update` or `apt install`. Bare-metal deployment, cluster expansion and remote image pulls do not depend on public OS repositories or an external container registry.

## Offline image delivery

The [offline image factory](deploy/offline/README.md) builds repository-owned Cloud Control, EPH and Target Agent images and packages every Metal3, IPA, Kubernetes and Calico image needed at runtime.

```bash
# On a connected build machine
python3 tools/offline_images.py fetch

# With the network disconnected
python3 tools/offline_images.py build --offline
```

- Versions and upstream references live in `deploy/offline/images.json`.
- `fetch` resolves image digests and records SHA256 for archives, binaries, wheels and IPA.
- `build --offline` uses `--pull=false` and `--network=none`; Agent Dockerfiles contain no apt/curl/wget installation path.
- EPH imports all archives into containerd, starts the registry Pod, publishes images and verifies digests before deploying the remaining workloads.
- Remote nodes pull only from the EPH local registry. No Docker Compose infrastructure stack and no external registry are used on EPH or Target nodes.

## Local application development

Docker Compose is only for the local application/control-plane development stack. It must never start, supervise or substitute for EPH or Target Agent Pods.

```bash
cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

- Vue Studio: http://localhost:5173
- Django API: http://localhost:8000/api/v1/
- Django Admin: http://localhost:8000/admin/
- Liveness: http://localhost:8000/health/live
- Readiness: http://localhost:8000/health/ready

Create the first administrator after the stack is running:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml exec api python apps/platform-api/manage.py createsuperuser
```

## Three-Agent deployment

See [Three-Agent deployment](deploy/agents/README.md) for image builds, secret requirements, mTLS certificate identities, Kubernetes manifests and rollout checks. See the [Target CAPI Pivot contract](docs/capi-pivot-contract.md) for approval, provider compatibility, controller placement and postflight gates.

Deployment requires three distinct Kubernetes contexts:

- Cloud management cluster (Alibaba Cloud or Tencent Cloud).
- Temporary EPH single-node Kubernetes cluster.
- Target Kubernetes cluster.

Build three independent Agent images and pin each by immutable SHA-256 digest. Apply Cloud Control Agent to the cloud management context, EPH Agent to the temporary EPH cluster and Target Agent to the Target cluster. Target Agent requires three schedulable Control Plane masters.

The Cloud Control API uses PostgreSQL and mTLS. The cloud load balancer must provide TCP/L4 TLS passthrough unless a separately trusted client-certificate identity propagation design is implemented.

## Validation and production status

Implemented foundations include tenant/project isolation, membership roles, token budgets, chained audit, Agent/Workflow APIs, connector approval, MCP streamable-HTTP runtime, Qdrant collection bootstrap and the three infrastructure Agent state machines.

Not yet production-complete:

- Subscription billing and plan management.
- MCP stdio transport, server mode and complete provider OAuth/token refresh.
- Realtime WebSocket/SSE event streaming.
- Loop Workflow nodes in the SaaS Platform editor. The Cloud Control workflow engine supports parallel branches (`join: "all" | "any"`), `condition` steps with `when`-guarded branches, bounded `loop` steps with `max_iterations`, human approval steps (`type: "approval"`), and per-run deadlines enforced by reconciliation. The SaaS Workflow Studio now inserts `approval` nodes, accepts `condition`/`agent` nodes through the JSON definition editor, supports `any/all` joins and offers approve/reject actions; bounded loop and per-run deadlines remain Cloud Control only.
- Multi-provider model routing and fallback chains.
- Redis-backed cache/queue coordination; pgvector and S3-compatible object storage.
- Real CAPI/Metal3/Ironic deployment, CAPI Pivot, BMH migration, EPH cleanup and physical Worker acceptance.
- Real SR-IOV/DPDK data-plane traffic validation and Day-2 scale/upgrade/repair operations.

The authoritative code-backed checklist is [Architecture Inventory (English)](docs/architecture-inventory.en.md) / [架构盘点（中文）](docs/architecture-inventory.zh-CN.md). Unit or mocked tests do not constitute proof of physical deployment success.

## Local checks

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

- `apps/platform-api`: Django SaaS API, tenancy, Agents, Workflows, audit and deployment records.
- `apps/worker`: asynchronous Agent, Agent Team and Workflow executor.
- `apps/cloud_control_agent`: cloud-side infrastructure control plane.
- `apps/eph_agent`: temporary EPH bootstrap Agent.
- `apps/target_agent`: Target takeover, CAPI Pivot and data-plane Agent.
- `packages/agent_contracts`: versioned Agent identity, command, lease, checkpoint and result contracts.
- `packages/agent_transport`: mTLS control transport.
- `apps/web-studio`: Vue 3 SaaS operator workspace.
- `deploy/agents`: three-Agent images and Kubernetes deployment assets.
- `deploy/offline`: offline image factory and version manifest.
- `eph-iso`: Debian 12 EPH Live OS bootstrap assets.
- `docs`: architecture, Agent Teams, threat model, verification matrix and delivery plans.

## Security baseline

Production rejects DEBUG mode and the development SECRET_KEY. DRF defaults to authenticated access; public endpoints must explicitly opt in. Tenant and project authorization is enforced server-side, never by model intent or client-supplied tenant identifiers. Model output, retrieved content, Skills, MCP responses and plugin data are untrusted.

No production CAPI Pivot, BMH mutation, EPH cleanup or Worker provisioning is enabled until its adapter, approval boundary, compatibility matrix and physical acceptance evidence are complete.
