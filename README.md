# AI Agent Stack

An extensible, policy-governed AI Agent platform with a Vue operator workspace, Django platform API, independent task Worker and a three-Agent bare-metal Kubernetes control system.

## Product direction

The long-term infrastructure product scope is documented in [Omni-Inspired Infrastructure Platform Roadmap](docs/omni-parity-roadmap.md). The bare-metal delivery path uses Cluster API (CAPI), Metal3 and Ironic. The authoritative Agent architecture is [ADR 0005: Fresh Three-Agent Runtime](docs/adr/0005-three-agent-fresh-runtime.md).

## Current application architecture

- **Frontend:** Vue 3, TypeScript, Vite, Element Plus, Pinia and Vue Router.
- **Platform API:** Python 3.12, Django, Django REST Framework and Django Channels/ASGI.
- **Persistence:** PostgreSQL is the authoritative business datastore; Redis is auxiliary.
- **Application task execution:** a separate Python Worker uses PostgreSQL-backed task records, atomic claims, lease tokens/expiry, heartbeat renewal, bounded retries and cooperative cancellation. It handles LLM/model routing and application workflows; it is not one of the three infrastructure Agents.
- **Integration Hub:** tenant-scoped versioned connector metadata, tool schemas, admin review, project installation, explicit permission grants and credential-reference records. Normalized HTTP/OpenAPI contracts and credential resolution are in progress; provider-specific connectors and MCP transport remain under development.
- **Knowledge and vector storage:** S3-compatible storage and pgvector are planned integration points.

## Authoritative three-Agent infrastructure architecture

1. **Cloud Control Agent** — runs in the Alibaba Cloud or Tencent Cloud management environment. Owns durable deployment/command state, approvals, global scheduling, task dispatch, status aggregation and failure coordination.

2. **EPH Agent** — runs as one Pod inside the temporary EPH Live OS / temporary single-node Kubernetes cluster. Owns source-side preflight and the source-side CAPI Pivot operation. EPH has exactly one temporary Kubernetes node and no Worker pool.

3. **Target Agent** — runs in the target Kubernetes cluster on Control Plane Master nodes as three replicas, spread across the masters. Kubernetes Lease leader election ensures only one replica claims and executes commands. Target Agent owns target adoption, CAPI/Metal3/Ironic coordination, BMH migration, EPH handoff/cleanup and Target Worker provisioning.

There is no per-Worker Agent and no Network Inventory Agent DaemonSet. Worker facts are collected through Kubernetes, CAPI, Metal3 and approved infrastructure APIs.

### Current fresh-runtime implementation

The new branch contains:

- Versioned Agent identity, command, lease, heartbeat, checkpoint and result contracts.
- mTLS Agent-to-Cloud Control communication with certificate identity bound to the registered Agent UUID.
- PostgreSQL-backed command registration, dispatch, lease claim/renewal, result reporting and Target checkpoint persistence.
- Target three-replica deployment, Control Plane scheduling constraints, Pod anti-affinity, PDB and Kubernetes Lease leader election.
- Read-only Kubernetes/CAPI/Metal3 observation.
- An ordered, fail-closed Target handoff state machine with exact BMH identity-set checks and durable compare-and-swap checkpoints.
- Control Plane chrony profile and pre-kubeadm command generation from approved external sources, plus Worker chrony configuration and pre-kubeadm command generation. Workers must use exactly the Ironic internal IP/VIP as their NTP source; no external pool or fallback is allowed. The generated host commands are not yet injected into CAPI bootstrap templates or executed on physical hosts.

**Not yet production-complete:** a new EPH CAPI Pivot adapter now supports dry-run and approval-digest-gated `clusterctl move`, including provider-version checks, Target Control Plane controller placement checks, source UID annotation and postflight identity verification. It has not yet been run against the selected live CAPI/Metal3/Ironic provider matrix. Autonomous EPH cleanup, Worker provisioning, applying the generated chrony command to actual hosts, full platform API/UI integration and physical BMC/Ironic acceptance remain outstanding. The new Ironic adapter is unit-tested against mocked HTTP responses; live Ironic credentials, CA trust, API microversion and physical BMC acceptance have not yet been verified. Other mutating actions fail closed. Unit tests or QEMU boot tests do not prove physical bare-metal provisioning.

The legacy `apps/node_agent` package and its shared runtime have been removed from this branch. The Cloud Control, EPH and Target Agents have independent source packages, entrypoints, images, credentials and command allowlists. No compatibility shim or fallback to the old Node Agent is permitted.

## Local application quick start

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

Create the first local administrator after the stack is running:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml exec api python apps/platform-api/manage.py createsuperuser
```

Docker Compose is for the local application/control-plane development stack. It must never start, supervise or substitute for EPH Agent or Target Agent Pods.

## Build and deploy the three infrastructure Agents

See [Three-Agent deployment](deploy/agents/README.md) for image builds, secret requirements, mTLS certificate identities, Kubernetes manifests and rollout checks. See the [EPH CAPI Pivot contract](docs/capi-pivot-contract.md) for approval, provider compatibility, controller placement and postflight gates.

Deployment requires three distinct Kubernetes contexts:

- Cloud management cluster (Alibaba Cloud or Tencent Cloud).
- Temporary EPH single-node Kubernetes cluster.
- Target Kubernetes cluster.

Build three independent images and pin each by immutable SHA-256 digest. Apply Cloud Control Agent to the cloud management context, EPH Agent to the temporary EPH cluster and Target Agent to the Target cluster. Target Agent requires three schedulable Control Plane masters.

The Cloud Control API uses PostgreSQL and mTLS. The cloud load balancer must operate as TCP/L4 TLS passthrough unless a separately trusted client-certificate identity propagation design is implemented.

## Ordered bare-metal delivery lifecycle

`eph_single_node_ready -> target_control_plane_ready -> target_agent_ready -> capi_pivot_verified -> bmh_registry_migrated -> eph_cleanup_verified -> worker_provisioning -> workers_ready -> completed`

- Target Control Plane masters are ready before Target Workers are provisioned.
- Target Agent restores the durable checkpoint after CAPI Pivot.
- BMH migration requires exact source/target equality of namespace, name and stable hardware identity, plus verified Target Metal3 ownership and reconciliation.
- EPH cleanup is autonomous Target Agent work and is blocked until CAPI ownership, BMH registry, controller health and EPH credential revocation are positively verified.
- Every external side effect requires idempotency, a durable checkpoint and postflight evidence. Missing or ambiguous evidence fails closed.

## NTP and network invariants

- EPH uses an independent bootstrap NTP source before EPH kubeadm initialization because Target Control Plane does not exist yet.
- Target Control Plane chronyd synchronizes from approved external NTP sources and exposes a reachable internal NTP service at the Ironic internal IP/VIP over UDP/123.
- Every Target Worker synchronizes only from that Ironic internal IP/VIP. External NTP servers and fallback pools are prohibited. Worker readiness must verify the actual selected chronyd source, not only local clock synchronization.
- Kubernetes Control Plane management uses ordinary NIC connectivity. The Data Plane is separate; Pod traffic uses the Data Plane, with Multus and SR-IOV/DPDK where required.

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

- `apps/platform-api`: Django platform API.
- `apps/worker`: independent application task Worker.
- `apps/cloud_control_agent`: Cloud Control Agent.
- `apps/eph_agent`: temporary EPH Agent.
- `apps/target_agent`: Target Agent, leader election, handoff and NTP policy.
- `packages/agent_contracts`: versioned, side-effect-free Agent contracts.
- `packages/agent_transport`: mTLS control transport.
- `apps/web-studio`: Vue 3 operator workspace.
- `deploy/agents`: independent image build and three-context Kubernetes deployment assets.
- `docs`: architecture decisions, threat model, verification matrix and delivery plan.

## Security baseline

Production rejects DEBUG mode and the development SECRET_KEY. DRF defaults to authenticated access; public endpoints must explicitly opt in. Model output, retrieved content, Skills, MCP responses and plugin data are untrusted. Authorization is enforced by server-side policy, never by model intent or client-supplied tenant identifiers.

No production CAPI Pivot, BMH mutation, EPH cleanup or Worker provisioning is enabled until its adapter, approval boundary, compatibility matrix and physical acceptance evidence are complete.
