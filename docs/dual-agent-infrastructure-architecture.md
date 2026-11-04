# Dual-Agent Infrastructure Control Architecture

## Purpose

This document extends the general AI Agent Studio architecture with a remotely managed, two-sided Kubernetes bare-metal delivery model. It does not replace the platform's general Agent, Workflow, Plugin, MCP, Skills or RAG capabilities.

## Components and trust boundaries

1. **Remote Control Plane Agent** runs in the central AI Agent Studio runtime. It receives the operator's request, creates a typed plan, checks tenant/project scope, resolves the target EPH node and target cluster, obtains required approvals, dispatches commands and aggregates progress. It is not a privileged shell on either node.
2. **EPH Node Agent** runs as a Pod inside a temporary single-node Kubernetes cluster on the EPH bare-metal node. The node boots a RAM-resident OS and is borrowed from the future target compute pool only for the bootstrap window. The local OS, Kubernetes etcd and Pod filesystem are disposable; the central platform is the durable source of truth. The EPH Agent establishes an outbound authenticated connection, performs preflight and temporary-cluster API inspection, and coordinates the approved bootstrap/handoff workflow. The physical node is later rebooted into its target worker image and reclaimed as a worker in the final cluster.
3. **Target Cluster Agent** is bootstrapped as a Pod only after the final target cluster API is reachable. It connects outbound to the same control plane, reports target-cluster health and executes only explicitly granted, typed target-side operations using a dedicated ServiceAccount. It also verifies that the reclaimed EPH physical machine has joined as a Ready worker.
4. **CAPI / Metal3 / Ironic / IPA / BMC** remain the infrastructure control chain. Any CAPI/Metal3 management components hosted on the EPH single-node Kubernetes cluster are temporary and must complete their required provisioning work before EPH reclamation. State required after EPH reboot must live in the central platform or a durable external management service; an ephemeral EPH management cluster cannot be treated as a persistent day-2 control plane.

## Bootstrap and reclamation lifecycle

The EPH-node is a temporary bare-metal bootstrap host booted into RAM. It starts a single-node Kubernetes bootstrap cluster in memory and runs the EPH Agent Pod. The target Control Plane is provisioned first. Once its API, etcd and core networking are healthy, the Target Agent is recreated as a Pod on a target control-plane node, resumes the durable deployment context from the central Supervisor, and proves it can execute commands. The CAPI management components and Cluster API objects are then pivoted from the EPH bootstrap cluster into the target cluster (using `clusterctl move` or a version-tested equivalent). The pivot must be verified before worker provisioning; moving only the Agent Pod does not transfer CAPI controllers or reconciliation state.

```text
Physical capacity pool
  -> reserve EPH hardware and network-boot RAM OS
  -> start single-node Kubernetes bootstrap cluster in memory
  -> run EPH Agent Pod and enroll with central Supervisor
  -> preflight + provider/CRD discovery + manifest validation
  -> provision target Control Plane only
  -> wait for target API, etcd and core networking Ready
  -> deploy Target Agent Pod onto target Control Plane (control-plane toleration)
  -> resume deployment/task state and verify Supervisor heartbeat
  -> pivot CAPI controllers and Cluster API objects from EPH to target cluster
  -> verify pivoted controllers and objects reconcile
  -> stop EPH Agent and temporary EPH bootstrap cluster; revoke EPH credentials
  -> provision remaining Control Plane replicas and Data Plane worker nodes
  -> Target Agent verifies cluster health and worker readiness
  -> re-image/reboot original EPH hardware as a target worker
  -> Target Agent verifies matching physical identity and Ready condition
  -> mark EPH reclaimed and deployment complete
```

The central Studio database is the durable source of truth. EPH local etcd, RAM OS, Pod filesystem, process memory, local kubeconfigs and temporary credentials are disposable. The new Target Agent Pod is a new process, not a live process-memory transfer; it resumes the same logical deployment/task from central durable state and receives a new target-cluster credential. CAPI pivot state must also be moved and verified independently. EPH heartbeat loss is expected only after the handoff checkpoint and credential revocation; it is not proof of successful pivot or worker reclamation.

The original EPH hardware is the last worker to be reclaimed after the target cluster has been built and verified, unless the operator explicitly chooses a different target-node disposition. The temporary EPH Kubernetes control plane must be stopped before that host joins the target cluster.

See [EPH Node Ephemeral Bootstrap and Reclamation Lifecycle](eph-node-lifecycle.md) for lifecycle invariants, suggested states, durable data requirements and recovery behavior.

## Cloud control plane and physical-node placement

The central Control Plane Agent is deployed independently in Alibaba Cloud or Tencent Cloud. The cloud deployment hosts the Studio API, model routing/orchestration, durable workflow state, approvals, agent registry, audit and command dispatch. It is not deployed inside the EPH bootstrap cluster and does not require direct inbound access to the bare-metal management network.

EPH Agent and Target Cluster Agent are deployed on the physical infrastructure:
- EPH Agent runs as a Pod in the temporary, RAM-booted single-node Kubernetes cluster on the reserved EPH physical server.
- Target Agent runs as a Pod on a target physical server after the target Kubernetes control plane and API are healthy.
- Both agents establish outbound TLS connections to the cloud Control Plane. Production connectivity should use mTLS or short-lived workload identity, certificate rotation and revocation. Do not expose Kubernetes API, Ironic API, BMC interfaces or agent listeners to the public Internet.
- The cloud-to-site path may use outbound Internet TLS, site-to-site VPN, private cloud connectivity or a controlled egress proxy. DNS, proxy, MTU, firewall and certificate trust must be validated from both physical environments.

The cloud Control Plane is the durable source of truth. It stores workflow checkpoints, command leases, approvals, deployment state and audit history so that EPH RAM reboot or loss of the temporary Kubernetes API does not erase the workflow.

## Dual-network Kubernetes and Pod traffic policy

The target Kubernetes cluster has distinct Control Plane and Data Plane networks. Network roles must be explicit in the deployment profile; interface names, VLANs, CIDRs, gateways, MTUs, routes and SR-IOV resource names are environment inputs and must never be guessed.

### Control Plane network

The ordinary physical NIC/control network carries node and cluster management traffic, including Kubernetes API access, kubelet-to-API communication, node heartbeats, CNI control operations, DNS/control services as designed, and the outbound Agent-to-cloud control channel. The cloud Control Plane must remain reachable even when the Data Plane is degraded.

Calico provides the cluster's ordinary Pod networking and policy/control integration according to the selected Calico mode. The selected mode (VXLAN, IP-in-IP, BGP or no-overlay/routed) must be declared and validated against the underlay. Do not assume Calico's default route or encapsulation behavior.

### Data Plane network

The Data Plane is used for workload traffic that is required by the deployment profile to use the high-performance network. Multus attaches additional interfaces to Pods through NetworkAttachmentDefinitions. OVS CNI and SR-IOV CNI are selectable attachments, not mutually exclusive cluster-wide replacements for the primary CNI:
- OVS provides virtual switching, VLAN/VXLAN/bridge connectivity and policy/QoS integration where configured.
- SR-IOV CNI attaches a VF allocated by the SR-IOV device plugin; resource requests/limits and VF pools must match the node hardware and resource inventory.
- DPDK workloads require a compatible userspace driver binding, hugepages, CPU/NUMA alignment, VFIO/IOMMU and device-resource configuration. SR-IOV CNI alone does not make a Pod DPDK-ready.
- Multus can attach more than one secondary network to a Pod. Each attachment must have a unique interface name and an explicit IPAM, MTU and routing policy.

The deployment profile's default is that Pod-originated traffic uses the Data Plane, including ordinary application Pods and Agent Pods. The primary Pod interface/default route must therefore be designed on the Data Plane; Multus adds further OVS/SR-IOV interfaces for workloads that need multiple isolated networks or dedicated high-performance queues. Node-originated management traffic (for example kubelet-to-API, host services and node-level CNI operations) uses the ordinary Control Plane NIC and its host routing table. Do not confuse node management traffic with Pod traffic. If a Pod must reach a Kubernetes API or cloud endpoint, provide a deliberate Data Plane route/firewall path or a narrowly scoped host-network/egress-gateway exception that is explicitly recorded; do not silently leak Pod traffic onto the Control Plane NIC.

### Implemented network inspection and plan validation

A dedicated `network-inventory-agent` DaemonSet runs on every target node, reads host `/sys` read-only, and publishes bounded per-node reports as ConfigMaps in `ai-agent-system`. The Target Agent aggregates these reports and correlates them with current Kubernetes Node UIDs so stale reports from replaced nodes are ignored. This is required because the main Target Agent is intentionally scheduled on a control-plane node and cannot directly inspect every worker host. The DaemonSet uses a separate ServiceAccount with only Node `get` and namespace-scoped ConfigMap `get/create/update` permissions.

The node-agent runtime currently exposes read-only `cluster.network.inspect` to both EPH and Target Agent roles. It reports Kubernetes version, node allocatable extended resources and hugepages, Multus NetworkAttachmentDefinitions/plugin type names, likely CNI component Pods in `kube-system`, the Agent Pod's own interfaces/default routes, and (when `/sys` is mounted read-only) physical NIC/PCI inventory including PF/VF counts, driver, IOMMU-group visibility, NUMA placement and hugepages. Its RBAC is limited to nodes/namespaces, NetworkAttachmentDefinitions and Pod listing in `kube-system` plus explicitly requested workload namespaces; it does not grant CNI mutation.

Cluster plan accepts an optional versioned `network_profile` and validates the Control Plane interface/CIDR/gateway/explicit MTU, Data Plane interface list/explicit MTU, `pod_traffic_default=data_plane`, Calico primary + Multus meta-CNI composition, OVS/SR-IOV attachment declarations, SR-IOV extended resource names and DPDK hugepage/NUMA/CPU Manager static-policy declarations. MTUs and interface names are mandatory environment inputs; the validator does not invent defaults. The canonical plan hash includes this profile.

This is a safe first implementation of inventory and input validation, not network provisioning or full dataplane proof. When `workload_namespaces` and `expected_attachments` are supplied, the command reads Pod network-status annotations in those explicitly scoped namespaces and reports missing Multus attachments per Pod. Access denied in a requested namespace is reported as unavailable, never as a pass. Network-status proves CNI attachment metadata, not end-to-end packet forwarding or default-route correctness. The Agent Pod's route table is not the host route table. Kubernetes allocatable resources do not prove physical VF health or DPDK binding. Actual host PCI/IOMMU/VFIO/NUMA checks, per-Pod route verification, packet reachability and CNI installation remain follow-on adapters and integration tests.

### CNI composition and validation order

Calico is the primary CNI for the Kubernetes network unless the selected platform design explicitly says otherwise. Multus is the meta-plugin that invokes the primary CNI and requested secondary CNI attachments. OVS CNI and SR-IOV CNI are additional network implementations selected per attachment. The cluster must not install competing primary-CNI configurations that both try to own the default `eth0`.

The network preflight and postflight contract must record and verify:
1. Node NIC and PCI inventory, PF/VF relationships, IOMMU/VFIO support, NUMA topology and hugepage capacity where DPDK is requested.
2. Control Plane interface/IP/gateway/MTU and stable Kubernetes API reachability.
3. Data Plane interface/VLAN/MTU/IPAM/gateway and underlay reachability.
4. Installed CNI/Multus/OVS/SR-IOV plugin versions and their Kubernetes compatibility.
5. NetworkAttachmentDefinition references, resource requests, Pod annotations, interface names and assigned IPs.
6. Pod route tables and policy routing, proving that Data Plane workloads use the intended Data Plane path and control traffic retains the Control Plane path.
7. Failure isolation: loss of the Data Plane must not sever cloud Agent heartbeat or Kubernetes API management connectivity.

The platform must treat the network configuration as a versioned, immutable deployment input and include its canonical hash in the plan and approval. Actual CNI installation or host networking changes remain disabled until versioned adapters and environment-specific schemas are implemented and tested.

## Agent communication

- Both node agents initiate outbound HTTPS requests; no inbound SSH port or public agent listener is required.
- Enrollment credentials are one-time and scoped to one tenant/project/node type. The initial implementation stores only a SHA-256 hash of the issued bearer token and returns the raw token once.
- Production deployment must use mTLS workload identity (or an equivalent short-lived certificate identity), credential rotation and revocation. The bearer-token transport is a development bootstrap mechanism and must be protected by TLS.
- Heartbeat reports agent version, node type, capabilities, current cluster identity and timestamp.
- Commands are durable PostgreSQL records with idempotency key, status, attempts, lease token, lease expiry, result and error summary. A node can only claim commands addressed to itself.
- Command acknowledgements require both node authentication and the current lease token. Stale workers cannot overwrite a newer lease's result.
- The initial command contract is typed and allowlisted. Arbitrary shell, arbitrary Python, arbitrary URLs, arbitrary kubeconfig, and raw manifest execution are rejected.
- Mutating operations such as cluster provisioning and target-agent bootstrap are created in `WAITING_APPROVAL`. Approval binds the canonical payload SHA-256, requires a different tenant administrator and expires after 15 minutes. The command becomes dispatchable only when the approved hash still matches and the approval is fresh.

## Typed command contract

EPH-side command families:
- `node.health`
- `host.preflight`
- `cluster.plan`
- `cluster.provision` (approval-gated)
- `target-agent.bootstrap` (approval-gated)
- `capi.cluster.observe`
- `metal3.host.inspect`

Target-cluster command families:
- `node.health`
- `cluster.health`
- `cluster.observe`
- `cluster.diagnose`
- `cluster.postflight`
- `k8s.resource.read`

A command schema validates the payload before it enters the queue and the node agent validates it again before dispatch. Capability declarations do not grant permissions; effective access is the intersection of node identity, project grants, command allowlist, approval and active policy.

## Cluster delivery state model

`EPH_RESERVED -> EPH_RAM_BOOTING -> EPH_LOCAL_K8S_READY -> EPH_AGENT_ENROLLED -> PREFLIGHT -> PLAN_READY -> WAITING_APPROVAL -> TARGET_CONTROL_PLANE_PROVISIONING -> TARGET_CONTROL_PLANE_READY -> TARGET_AGENT_DEPLOYING -> TARGET_AGENT_READY -> CAPI_BOOTSTRAP_PIVOT_PENDING -> CAPI_BOOTSTRAP_PIVOT_VERIFIED -> WORKER_PROVISIONING -> WORKERS_READY -> EPH_HANDOFF_PENDING -> EPH_REBOOTING -> EPH_TARGET_WORKER_JOINING -> EPH_RECLAIMED -> SUCCEEDED`

Any step may fail with a stable error code and diagnostic reference. A retry is allowed only when the operation is idempotent or the adapter can reconcile the external state. A timeout does not imply that an external provisioning operation did not happen. Deletion, BMC power control, disk wipe and production network changes require separate critical-risk policies and explicit approvals.

## Version and compatibility policy

The EPH Agent must report management-cluster Kubernetes version, CAPI provider versions, Metal3/Ironic versions, installed CRD API versions and enabled capabilities. The target cluster agent reports target Kubernetes version and supported API resources. The control plane selects only adapters whose compatibility matrix has been verified. No CAPI/Metal3/Ironic API version, BMH field or vendor-specific Redfish behavior is guessed.

Before enabling real provisioning, the operator must configure and verify:
- management cluster endpoint and CA;
- exact Kubernetes, CAPI core/provider, Metal3, Ironic and IPA versions;
- supported CRD schemas and template source;
- image, network, IPAM, DNS and BMC credential references;
- allowed namespaces, resource names, verbs and cluster/project scope;
- rollback/recovery and postcondition rules.

## Current implementation boundary

The current code supports node enrollment, heartbeat, durable command dispatch, lease-protected results and hash-bound approval. The complete RAM-boot, temporary single-node Kubernetes lifecycle, Target Agent deployment onto the target Control Plane, durable task resume, CAPI bootstrap-cluster pivot, worker provisioning gate, physical identity correlation, EPH-to-worker handoff, reboot/re-image, and reclaim state machine are not implemented yet. The EPH runner supports safe health/preflight/plan commands. A separately enrolled Target Cluster Agent can now receive a read-only `cluster.postflight` command after an operator explicitly confirms that the cluster was provisioned externally and the EPH plan has no missing prerequisites. The postflight result marks the deployment successful only when at least one node is present and all nodes report Ready. Actual CAPI/Metal3 resource writes, Ironic/BMC power operations and automated target-agent installation remain disabled until a concrete environment compatibility matrix and adapters are supplied and tested.

## Kubernetes agent deployment

Kubernetes templates are under `deploy/agents/`:
- `namespace.yaml`: shared `ai-agent-system` namespace;
- `rbac.yaml`: separate EPH and target ServiceAccounts with read-only CAPI/Metal3 or target workload inspection scopes;
- `eph-node-agent.yaml`: EPH Agent Deployment in the temporary single-node Kubernetes cluster booted on the RAM-resident EPH node;
- `target-cluster-agent.yaml`: target agent Deployment, applied after target API readiness and pinned to a control-plane node with the required NoSchedule tolerations so it can run before any workers exist;
- `README.md`: image, one-time token Secret and rollout instructions.

Both Deployments run non-root, drop Linux capabilities, disable privilege escalation, use a read-only root filesystem and mount only a small writable `/tmp` volume for liveness/readiness timestamps. The target workload Role is scoped to the `default` namespace as an example; create additional namespace-local RoleBindings only for approved namespaces. No Service or inbound listener is exposed.

## Cluster plan input contract

The EPH `cluster.plan` command validates the basic cluster identity, Kubernetes version, odd control-plane quorum and worker count. It also reports missing production prerequisites:
- exact CAPI core, Kubeadm bootstrap/control-plane, Metal3 infrastructure, Metal3, Ironic and IPA versions;
- Pod/Service CIDRs and control-plane endpoint;
- machine image URL and checksum;
- Metal3 namespace and enough named BareMetalHost references for all requested replicas;
- BMC credential references, never plaintext BMC credentials.

A complete input checklist is not a compatibility pass. `provisioning_ready` means required fields are present; `adapter_enabled` remains false until the exact target CRD schemas and provider versions are tested and the provisioning adapter is implemented. No manifest is applied by this plan command.


## Persisted cluster plan workflow

The Studio's Cluster Delivery Plan API creates a `ClusterDeployment` record scoped to one tenant/project and an online EPH agent. It enqueues `host.preflight`. A successful EPH preflight automatically enqueues `cluster.plan`; the returned typed plan, missing prerequisites and plan hash are persisted. The Vue console displays each stage and allows cancellation while the workflow remains in its plan-only phase.

This workflow deliberately stops at `PLAN_READY` and does not enqueue a provisioning write command. The Target Cluster Agent postflight endpoint and result handling are implemented, but dispatch is deliberately gated on a future provisioning adapter setting `adapter_enabled: true`, `provisioning_completed: true` and deployment status `WORKERS_READY`. The current dry-run plan reports `adapter_enabled: false`, so the target postflight button remains unavailable. When enabled, the postflight command will receive only the deployment ID and cluster name, not the full provisioning spec or BMC credential references.


## Upstream compatibility references (reviewed 2026-11-02)

The CAPI core support matrix defines supported management/workload Kubernetes minors per CAPI release, while infrastructure providers maintain their own compatibility contracts. CAPM3's own release table currently lists different API contracts across provider minors (for example, older tested releases and newer supported releases do not necessarily use the same API version). Therefore the target cluster's Kubernetes version alone is not enough to choose a CAPI/CAPM3 API version.

- [Cluster API version support and Kubernetes skew](https://cluster-api.sigs.k8s.io/reference/versions)
- [Metal3 provider version support](https://book.metal3.io/version_support)
- [Cluster API Provider Metal3 repository and compatibility notes](https://github.com/metal3-io/cluster-api-provider-metal3)

The agent discovers API versions from the connected management cluster and the plan requires explicit provider version references. The platform must still validate the actual installed CRD schemas, CAPI contract, CAPM3/IPAM versions, Bare Metal Operator, Ironic and IPA versions, and the user's machine templates before enabling writes. This upstream documentation review is not a substitute for an isolated environment integration test.


## Central AI supervisor and remote execution agents

The intended control topology has three agent roles:
1. **Control-plane AI supervisor**: interprets the user's request, gathers environment context, proposes a typed deployment plan and asks the policy/approval service to authorize it.
2. **EPH Node Agent**: runs in the EPH/management Kubernetes environment, performs preflight, discovers the live CAPI/Metal3 API contract and executes only compiled allowlisted management-cluster operations.
3. **Target Cluster Agent**: is installed after the target Kubernetes API becomes available, performs target-side readiness/postflight and later receives only target-cluster-scoped operations.

Remote agents do not receive free-form model-generated shell or raw manifests. The control-plane supervisor must convert its plan into a versioned command schema; the server validates tenant/project scope, tool capability, risk, idempotency and approval hash before dispatch. The current implementation has the EPH/Target enrollment and command plane plus persisted EPH preflight/plan workflow. The EPH environment is a temporary RAM-booted single-node Kubernetes cluster, not a permanent management cluster; the same hardware must later be re-imaged/rebooted and verified as a target worker. The central LLM-to-node command tool bridge, target-agent bootstrap and CAPI/Metal3 write adapter are still unimplemented.


## Deployment state and completion safeguards

The current EPH planner is a read-only dry-run adapter. A plan may be stored as `PLAN_READY`, but this does not mean CAPI/Metal3/Ironic provisioning has happened. The API rejects target postflight unless the deployment has an explicit completed-provisioning record, the plan states that a real adapter is enabled, and the workflow has reached `WORKERS_READY`. This prevents a dry-run from being reported as a successful Kubernetes installation.

The platform validates the EPH plan hash against the exact immutable cluster specification, checks cluster name/version and replica counts, and validates the preflight CPU count before advancing the deployment state. Target postflight results must come from a Target Cluster Agent and include a positive integer node count with every node Ready. Agent command polling also enforces each enrolled node role's command allowlist, including for commands inserted by internal workflows. Target postflight retries reuse the same idempotent command.

**Provisioning limitation:** the production CAPI/Metal3/Ironic mutation adapter and its provider-specific manifests are not enabled in this milestone. `cluster.provision` remains approval-gated and the node-agent runner refuses to execute it without a configured adapter. Do not interpret `provisioning_ready` from a dry-run as a provisioning-completed signal.


## Live Provider API Discovery (read-only)

The EPH command `cluster.provider.discover` queries the connected management API's discovery endpoints and reports the live preferred API version plus required resource availability for CAPI core, Kubeadm bootstrap, Kubeadm control plane, CAPM3 and Metal3. It does not infer provider release versions from Kubernetes API versions. The result separately marks Ironic, IPA, BMC reachability and machine-image verification as unchecked because those are not established by Kubernetes API discovery. Missing CRDs/resources are returned as stable `missing_requirements` entries.

This is a read-only capability check, not a provider compatibility pass: CRD OpenAPI schema validation, controller deployment health, provider release/image verification, manifest generation, server-side dry-run apply and all mutations remain disabled. A successful discovery result must never set `provisioning_ready`; the current command explicitly returns `provisioning_ready: false` and `mutation_adapter_enabled: false`.


## Provider CRD OpenAPI inspection

The EPH Agent provider discovery operation now fetches the installed CRD objects for the required CAPI core, Kubeadm bootstrap/control-plane, CAPM3 and Metal3 resources. It reports served versions, storage version flags, CRD scope, top-level `spec` property names and required `spec` fields without returning the full CRD schema. Missing CRDs and served versions without an OpenAPI v3 schema are reported separately. This is a schema-presence and shape summary only; it does not prove provider release compatibility or validate a proposed manifest against every nested schema constraint. The EPH read-only ClusterRole grants `get` on `apiextensions.k8s.io/customresourcedefinitions`; no create, patch, update or delete permission is added. Provisioning remains disabled.


## Live CRD-backed manifest validation (read-only)

The EPH Agent exposes `cluster.manifest.validate`. It accepts a bounded list of candidate custom-resource manifests, resolves each Group/Kind against the installed CAPI, Kubeadm, CAPM3 and Metal3 CRDs, requires the requested API version to be served, checks CRD scope and recursively validates supported OpenAPI schema constraints (types, required fields, enum, string length/pattern, numeric bounds, array item/count constraints, nested properties and additionalProperties). Results contain bounded per-manifest validation errors and never return the CRD schema itself.

The operation is read-only: it uses only CRD GET requests and does not submit manifests to the Kubernetes API. It is not Kubernetes API server admission validation and does not execute CEL rules, conversion webhooks, admission webhooks, cross-resource reference checks or provider compatibility tests. The EPH Role remains read-only. Candidate manifests are limited to 100 objects and a 1 MiB serialized payload. This validation is an additional preflight gate, not authorization to provision; `server_side_dry_run`, `mutation_adapter_enabled` and `provisioning_ready` remain false.


## EPH temporary-node ownership and reclaim boundary

The EPH machine is borrowed from the final compute pool. It must not be counted as a target worker before the Target Agent observes the same verified physical identity as a Ready node. EPH Agent identity and Target Agent identity are separate and have separate short-lived credentials. EPH going offline during an approved handoff is an expected event; central deployment state must wait for target-side confirmation rather than interpreting the lost heartbeat as completion.

Any management components hosted in the EPH single-node Kubernetes cluster are temporary. If CAPI/Metal3 controllers or management objects are hosted there, all required provisioning and reconciliation for this bootstrap must finish before EPH is reclaimed. Persistent day-2 management cannot depend on the EPH cluster's etcd or local disk. See [the dedicated lifecycle specification](eph-node-lifecycle.md).
