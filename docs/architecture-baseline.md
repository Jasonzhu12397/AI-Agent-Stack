# Architecture baseline V3.1

## Technology decision

- Frontend: Vue 3 + TypeScript + Vite + Element Plus + Pinia + Vue Router.
- Backend: Python 3.12 + Django + Django REST Framework.
- Realtime: Django Channels on ASGI; durable run streaming is part of the platform API boundary.
- Persistence: PostgreSQL is authoritative. Redis is auxiliary and never the only store for durable state.
- Execution: independent Python Workers; task claiming, leases and recovery are separate from the three infrastructure Agent runtimes.
- Object storage: S3-compatible API. Vector retrieval: pgvector behind an abstraction.
- Deployment: Docker Compose locally; Kubernetes with Helm/Kustomize in production.

## Runtime boundaries

Django is the platform control plane: identity, tenant/project authorization, configuration, version publication, policy, audit and API orchestration.

Python Workers are the general execution plane: Agent runs, workflow steps, scheduled work and isolated integration execution. Long-running work must not be tied to an HTTP request lifecycle.

Vue communicates with Django through versioned REST APIs and WebSocket channels. The browser is never authoritative for tenant identity, permissions, deployment state or execution policy.

## Initial platform implementation status

The platform includes the Django ASGI application, DRF APIs, health endpoints, Vue 3 application shell, PostgreSQL persistence and a standalone Python Worker. Authentication, durable task orchestration, model routing, plugin execution and workflow capabilities must be assessed independently against their implementation and integration tests; the presence of an application shell is not proof that those capabilities are production-complete.

## Security invariants

- Production refuses the development secret key and DEBUG mode.
- DRF defaults to authenticated access; intentionally public endpoints must explicitly opt in.
- Server derives Principal and tenant scope; client-supplied tenant identifiers are untrusted.
- Model output, retrieval results, Skill content, MCP responses and plugin data are untrusted.
- Credentials are stored as secret references, never copied into prompts, logs or traces.
- Mutating infrastructure operations use typed commands, scoped identities, durable checkpoints and explicit risk policy. Target takeover is an autonomous Target Agent lifecycle operation, not a Cloud Control approval/review workflow.
- Retries of non-idempotent side effects are not automatic; ambiguous outcomes must be reconciled against the external system before retry.

## Deployment boundaries

API and Worker are separate processes built from the platform Python packages. Web Studio is a separate Node/Vite application. PostgreSQL owns durable business state; Redis supports cache and coordination only. Production secrets must come from an external secret manager.

## Three-Agent bare-metal Kubernetes control system

The infrastructure delivery system has exactly three independent Agent roles. The legacy shared Node Agent is retired and is not an allowed runtime, package, image, deployment, compatibility layer or source of business logic.

1. **Cloud Control Agent** runs independently in Alibaba Cloud or Tencent Cloud. It receives user intent, creates and persists the deployment plan, performs tenant/project authorization, schedules work, dispatches typed commands and aggregates progress. It does not approve or review Target takeover and does not make BMH ownership decisions.
2. **EPH Agent** runs as a Pod in one temporary RAM-booted, single-node EPH Kubernetes cluster. It performs bootstrap discovery, host/network preflight and source-cluster operations while the temporary environment exists. EPH is one bootstrap cluster, not one Agent or cluster per physical server.
3. **Target Agent** runs as a three-replica HA service on the target Kubernetes Control Plane master nodes. Leader election ensures one active executor. Once target Control Plane readiness is proven, Target Agent autonomously coordinates CAPI/Metal3 ownership handoff, BMH registration migration, EPH workload and temporary-cluster cleanup, and then Worker provisioning. It resumes from durable checkpoints and verifies each external side effect.

The delivery chain is Kubernetes Cluster API (CAPI) + Metal3 + Ironic + bare metal. Do not replace this architecture with Omni or another cluster manager. The general AI Agent Studio, model routing, workflows, plugins, MCP, Skills and RAG remain separate extensible platform capabilities.

## Bootstrap, handoff and Worker sequence

1. Reserve one physical server as the temporary EPH bootstrap host and RAM-boot its operating system.
2. Start exactly one single-node EPH Kubernetes cluster and enroll the EPH Agent.
3. Run host, network, provider, CRD and deployment-profile preflight.
4. Provision the target Kubernetes Control Plane through CAPI, Metal3 and Ironic.
5. Verify target API Server, etcd, core networking and Control Plane Nodes.
6. Deploy the three-replica Target Agent onto distinct target Control Plane master nodes and verify its live leader Lease.
7. Target Agent resumes the durable deployment context, verifies source/target cluster identity, coordinates CAPI pivot and BMH registry ownership migration, and verifies target-side Metal3/Ironic ownership.
8. Target Agent revokes temporary EPH credentials and cleans up the EPH Agent and temporary bootstrap environment only after positive handoff evidence is present.
9. Target Agent provisions remaining Control Plane capacity where required and all Data Plane Workers through CAPI/Metal3/Ironic.
10. Verify Worker Node readiness, BMC/Ironic identity, network attachments, data-plane routes and NTP synchronization before marking deployment successful.

A timeout or lost heartbeat is not proof that a provisioning, pivot, migration or deletion operation failed or succeeded. Each phase needs idempotent reconciliation and a postflight check. The central platform database stores durable task and checkpoint state; temporary EPH etcd, RAM filesystem, Pod filesystem and local credentials are disposable.

## NTP invariants

- EPH uses its configured bootstrap NTP source because the target Ironic endpoint does not yet exist.
- Target Kubernetes Control Plane nodes synchronize with explicitly configured external NTP sources.
- The Ironic internal API IP/VIP is the NTP source exposed to Workers.
- Every Worker synchronizes chronyd only from the Ironic internal IP/VIP. Worker bootstrap must not contain public NTP pools, external fallback sources or the Ironic external IP.
- Validate reachability and actual chronyd synchronization before kubeadm join and again during postflight.

## Control Plane and Data Plane networking

- Control Plane node management traffic uses the ordinary Control Plane NIC.
- Pod-originated workload traffic defaults to the Data Plane as required by the deployment profile.
- Calico is the primary Kubernetes CNI; Multus attaches secondary networks. OVS CNI and SR-IOV CNI are selected per NetworkAttachmentDefinition.
- SR-IOV device-plugin pools, node selectors, tolerations, ConfigMaps and extended resource requests must map to the physical PF/VF inventory.
- DPDK readiness additionally requires validated IOMMU/VFIO, driver binding, hugepages, CPU/NUMA alignment and packet-path tests. SR-IOV attachment metadata alone is not proof of DPDK dataplane readiness.
- Interface names, VLANs, CIDRs, gateways, MTUs, IPAM and routes are deployment inputs. Never infer site-specific values.

## Implementation and acceptance boundary

A passing unit test proves only the behavior covered by that test. It does not prove live CAPI pivot, Metal3 ownership migration, Ironic/BMC provisioning, EPH reclamation, SR-IOV VF allocation, DPDK packet forwarding or physical NTP synchronization. Each production claim must identify its exact environment, operation and observed postcondition.

See [ADR 0005: Fresh Three-Agent Runtime](adr/0005-three-agent-fresh-runtime.md), [Three-Agent Bare-Metal Kubernetes Delivery Architecture](dual-agent-infrastructure-architecture.md) and [Three-Agent deployment](../deploy/agents/README.md).
