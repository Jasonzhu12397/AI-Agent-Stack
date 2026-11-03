# EPH Node Ephemeral Bootstrap and Reclamation Lifecycle

## Core definition

The EPH-node is a temporary bare-metal bootstrap worker borrowed from the capacity that will ultimately belong to the target Kubernetes cluster. It is not a permanent management server and must not be modeled as a long-lived infrastructure control plane.

The EPH-node boots a RAM-resident operating system (for example, a PXE/iPXE- or equivalent network-booted image). Its local disk is not the source of truth and must not be required for recovery. It starts a single-node Kubernetes cluster in memory and runs the EPH AI Agent as a Pod in that cluster. The EPH Agent is the temporary on-node execution endpoint controlled by the central AI Agent Studio.

The same physical machine is reclaimed after bootstrap and joins the final target Kubernetes cluster as a compute/worker node. It is not intended to remain a separate EPH management node.

## Roles

- **Central AI Agent Studio / Supervisor:** durable source of truth for deployment plans, approvals, command history, physical-node identity, lifecycle state, credentials references, and audit events. It remains available when the EPH machine reboots or its RAM-based OS is destroyed.
- **EPH RAM OS + single-node Kubernetes:** temporary execution environment. It hosts the EPH Agent Pod and any explicitly required temporary bootstrap components. All local cluster state is disposable.
- **EPH Agent Pod:** performs host checks, reads the temporary Kubernetes API, discovers installed CAPI/Metal3 CRDs, validates candidate manifests, and coordinates the approved bootstrap workflow. It must not assume that its pod filesystem, Kubernetes etcd, or local credentials survive reboot.
- **Target Kubernetes cluster:** the final workload cluster. Its Target Agent runs as a Pod after the target API and worker/control-plane scheduling are ready.
- **Physical node identity:** the same hardware must be correlated across EPH enrollment and target-worker enrollment using a stable inventory identity (for example, a verified system UUID/BMC inventory identifier). Do not use a transient Pod name, DHCP address, or MAC address as the only identity.

## Lifecycle

```text
Capacity pool / physical inventory
  -> reserve one bare-metal node for temporary EPH use
  -> network-boot RAM OS
  -> start single-node Kubernetes in RAM
  -> start EPH Agent Pod and enroll with central Supervisor
  -> preflight hardware, network, image and target plan
  -> validate candidate CAPI/Metal3 manifests against live CRD schemas
  -> approved provisioning of the target control plane only
  -> target control plane API, etcd and core networking become Ready
  -> deploy the Target Agent Pod onto a target control-plane node
  -> restore the durable deployment/task checkpoint and establish Supervisor heartbeat
  -> verify Target Agent identity, API access, lease ownership and command execution
  -> pivot CAPI management components and Cluster API objects from the EPH bootstrap
     cluster into the target cluster (clusterctl move or a version-tested equivalent)
  -> verify CAPI controllers are healthy and the moved Cluster/ControlPlane objects reconcile
  -> only now stop the EPH Agent and temporary EPH Kubernetes bootstrap cluster
  -> invalidate EPH runtime credentials and persist the handoff checkpoint centrally
  -> provision remaining control-plane replicas (if any) and Data Plane worker nodes
     from the now self-hosted target management plane
  -> Target Agent verifies all requested nodes and networking are Ready
  -> reboot/re-network-boot the same physical EPH host into the target worker OS
  -> enroll that physical host with a new Target Agent identity and join as a worker
  -> Target Agent verifies the expected physical identity and Ready condition
  -> mark the EPH hardware reclaimed and return it to target compute capacity
```

The Target Agent runtime now discovers deployments assigned to its enrolled node through the central Supervisor heartbeat. For each newly assigned deployment it retrieves the central checkpoint over the authenticated agent API and retains the validated checkpoint in process memory; the API returns only the assigned deployment and checkpoint metadata/data, never local EPH state or credentials. The runtime emits a structured `deployment_checkpoint_restored` event containing the deployment ID, phase, sequence and resume flag. Checkpoint writes use a monotonically increasing sequence, reject replay/conflicting reuse, restrict lifecycle phases, cap payloads at 32 KiB, and reject credential/executable fields. The checkpoint is still an orchestration recovery record, not a snapshot of a running process; command-specific resume handlers and automatic CAPI pivot execution remain separate implementation work. The current checkpoint generation is fixed at 1 and does not yet provide a rotating handoff lease or multi-generation agent identity transfer.

The Target Agent exposes the read-only `cluster.control-plane.verify` command. It checks the live Kubernetes version and Node objects, counts only nodes carrying the `node-role.kubernetes.io/control-plane` or legacy master role label, and requires every discovered Control Plane node to be Ready and the configured expected replica count to be met. Its result explicitly gates both `capi_pivot_allowed` and `worker_provisioning_allowed`; it performs no writes. For the bootstrap handoff, set `expected_control_plane_replicas` to the number of Control Plane nodes that must exist at that stage (normally 1 for the first-CP pivot), not the final desired count when additional Control Plane replicas are intentionally deferred.

The target Control Plane must be operational before the EPH Agent is moved. The Agent is recreated as a Pod in the target cluster; its process memory and Pod filesystem are not migrated. The central Supervisor is the durable task source of truth, so the new Pod resumes using the same logical deployment/task identity and a fresh target-cluster credential. Schedule it onto a control-plane node with the standard control-plane NoSchedule toleration and an explicit control-plane node selector, because no workers exist yet.

The CAPI bootstrap-cluster pivot is a separate and mandatory operation. If CAPI controllers and Cluster API objects live in the temporary EPH Kubernetes cluster, merely moving the AI Agent Pod does not move CAPI ownership or reconciliation. Before shutting down EPH, move the CAPI management components and owned Cluster API objects into the target cluster using `clusterctl move` (or a provider/version-tested equivalent), then verify controllers and objects reconcile there. Only after that pivot is healthy may the workflow create the remaining control-plane replicas and Data Plane workers. The exact pivot procedure must be tested against the selected CAPI/CAPM3 versions and provider topology.

The exact OS transition mechanism (new RAM image, disk-backed target OS, or another approved worker image) is an environment-specific implementation detail. It must be explicitly configured and tested; the platform must not assume that a running EPH OS can simply be converted in-place into a target worker.

## Critical lifecycle invariants

1. **EPH is temporary:** EPH-node capacity is reserved only for the bootstrap window. It must not be counted as steady-state target capacity until the Target Agent confirms that the same physical machine has joined and is Ready.
2. **No local durability assumptions:** deployment state, plans, approval hashes, audit logs, physical identity mapping, image references, and recovery checkpoints are persisted in the central platform or another durable external service before the EPH node is reclaimed.
3. **RAM loss is expected:** power loss, reboot, or RAM reset destroys the EPH operating system and its single-node Kubernetes state. Recovery resumes from the central deployment record and physical inventory, not from local etcd or the EPH Pod filesystem.
4. **Separate agent identities:** EPH enrollment and Target Agent enrollment are separate identities with separate scopes and credentials. EPH credentials are revoked/expired during handoff. Target enrollment uses a new short-lived token/certificate.
5. **No premature success:** target cluster readiness does not mean EPH reclamation is complete. The deployment is complete only after the Target Agent confirms the EPH physical node is a Ready worker in the target cluster, or an explicitly configured policy records a different disposition.
6. **Expected EPH disconnect:** the EPH Agent going offline during an approved handoff is an expected lifecycle event, not automatically a failed deployment. The Supervisor tracks a handoff deadline and waits for Target Agent confirmation. A timeout becomes a recoverable handoff failure requiring reconciliation.
7. **No overlapping control planes:** the temporary single-node EPH Kubernetes cluster must be stopped before the same host joins the target cluster. Do not run both kubelets/control planes concurrently on the same host.
8. **No secret carry-over:** temporary Kubernetes credentials, bootstrap tokens, kubeconfigs and ephemeral secrets must not be copied into the final worker environment. Only the minimum short-lived target join material is delivered through the approved bootstrap channel.
9. **CAPI bootstrap pivot before workers:** if CAPI controllers and Cluster API objects are hosted inside EPH, they must be moved into the target cluster and verified healthy before EPH shutdown and before worker provisioning. Moving only the AI Agent Pod is insufficient; it does not transfer CAPI ownership or reconciliation state. The target cluster becomes the durable self-hosted CAPI management plane, unless a separate external management cluster is explicitly configured.
10. **No premature worker provisioning:** worker provisioning must not start until the target Agent is healthy on the target Control Plane and the CAPI pivot has completed and reconciled successfully.
11. **Idempotent reclaim:** handoff, EPH credential revocation, target worker enrollment, and completion reporting must be idempotent and bound to the same deployment ID and physical inventory identity.

## Suggested deployment states

```text
EPH_RESERVED
  -> EPH_RAM_BOOTING
  -> EPH_LOCAL_K8S_READY
  -> EPH_AGENT_ENROLLED
  -> PREFLIGHT
  -> PLAN_READY
  -> WAITING_APPROVAL
  -> TARGET_PROVISIONING
  -> TARGET_CONTROL_PLANE_READY
  -> TARGET_AGENT_DEPLOYING
  -> TARGET_AGENT_READY
  -> CAPI_BOOTSTRAP_PIVOT_PENDING
  -> CAPI_BOOTSTRAP_PIVOT_VERIFIED
  -> WORKER_PROVISIONING
  -> WORKERS_READY
  -> EPH_HANDOFF_PENDING
  -> EPH_REBOOTING
  -> EPH_TARGET_WORKER_JOINING
  -> EPH_RECLAIMED
  -> SUCCEEDED
```

Terminal or recoverable failure states must preserve the last durable checkpoint and distinguish provisioning failure from worker-reclaim failure. A target cluster may be usable while the EPH node remains unreclaimed; that is a partial outcome, not full success.

## Control-plane API and data requirements

The central platform should persist at least:

- deployment ID and immutable deployment specification/hash;
- physical inventory ID and verified hardware identity;
- EPH reservation ID, boot session ID, RAM image version and local Kubernetes version;
- EPH agent enrollment ID, boot/session identity, heartbeat and lease expiry;
- target cluster identity, endpoint and target Kubernetes version;
- CAPI/Metal3/Ironic/IPA versions and external service references needed during bootstrap;
- target-agent enrollment and postflight result;
- handoff intent, expected reboot window, worker bootstrap reference, and reclaim deadline;
- observed target node UID/name, hardware identity match and Ready condition;
- credential revocation and audit timestamps.

Do not persist raw BMC passwords, kubeconfigs, bearer tokens, or join tokens in deployment JSON. Store secret-manager references and short-lived credential handles.

## Implemented Target Agent handoff API boundary

The platform now exposes a Control-Plane-first handoff binding endpoint:

- An authenticated project editor calls `POST /api/v1/cluster-deployments/{deployment_id}/target-agent/handoff/` with the enrolled Target Agent node ID.
- The endpoint accepts only `CONTROL_PLANE_READY` or an idempotent `TARGET_AGENT_PENDING` deployment, requires a fresh Target Agent heartbeat, and binds exactly one target identity to the deployment.
- The response includes the durable checkpoint endpoint and explicitly returns `worker_provisioning_allowed: false`.
- The Target Agent reads or advances its central checkpoint through `GET/POST /api/v1/agent-runtime/deployments/{deployment_id}/checkpoint/`, authenticated with its own enrollment credential.
- Checkpoints are scoped to the bound Target Agent identity and deployment, use generation 1 and monotonically increasing sequence numbers, support exact-request idempotency, and reject replay, stale agent heartbeats, credential fields, and executable payload fields. The checkpoint payload is capped at 32 KiB.

This is a durable checkpoint storage and restore API, not yet automatic Pod deployment or automatic CAPI pivot. The caller must currently ensure that the first Control Plane readiness transition to `CONTROL_PLANE_READY` has been independently verified. The CAPI pivot executor, checkpoint-driven workflow scheduler, and worker provisioning gate remain to be implemented.

## Read-only CAPI Pivot target preflight

The Target Agent now exposes `cluster.capi.pivot.target.preflight`. This command is intentionally read-only and validates the destination management plane before any pivot executor can be considered:

- Discover the served API versions for Cluster API, Kubeadm Bootstrap, Kubeadm Control Plane, CAPM3 and Metal3.
- Confirm the required Cluster, Machine, KubeadmConfig, KubeadmControlPlane, Metal3Cluster, Metal3Machine, Metal3MachineTemplate and BareMetalHost resources are served.
- Confirm the CAPI core, Kubeadm bootstrap, Kubeadm control-plane and CAPM3 controller Deployments have all desired replicas available and their observed generation is current.
- Check that the requested cluster name does not already exist in the destination namespace.
- Return explicit blockers and `pivot_execution_allowed`; missing APIs, unavailable controllers or a cluster-name collision fail closed.

This is only the target-side preflight. It does not inspect the EPH source ownership graph, copy credentials, run `clusterctl move`, transfer provider objects, or prove post-pivot reconciliation. A source-side inventory and version/provider compatibility check are still required before an executor can be enabled.

The Control Plane-first handoff endpoint now queues `cluster.capi.pivot.target.preflight` directly to the assigned Target Agent. The API validates the returned cluster/namespace, exact four-controller inventory, controller readiness, API blockers and the no-side-effects flag before persisting the result in the deployment plan. A successful preflight is recorded as `preflight_validated`; it deliberately does not advance the deployment to worker provisioning or authorize a pivot mutation.

The same handoff now queues `cluster.capi.pivot.source.preflight` to the EPH Agent. It performs read-only discovery of CAPI core, Kubeadm bootstrap/control-plane, CAPM3 and Metal3 APIs; lists required resource kinds across namespaces; records bounded object identity/owner-reference summaries and resource counts; and checks source management-controller Deployments. Missing APIs, incomplete pagination, truncated inventory, unavailable controllers or malformed result data fail closed. The Supervisor validates the EPH node binding, provider API versions, the complete required resource-count key set, object summaries and no-side-effects flag before persisting `capi_pivot_source_preflight`. Both source and destination results are evidence only; neither authorizes `clusterctl move` or worker provisioning.

The destination preflight returns the same five provider API-group versions and checks the complete source inventory resource set, including MachineSets and KubeadmConfigTemplates. The Supervisor validates the destination version map and, once both sides have independently passed their read-only checks, compares every provider group version. The comparison is persisted as `capi_pivot_provider_compatibility`, including explicit source/target mismatches. A version mismatch prevents compatibility from being marked true. Even an exact match is evidence only: `execution_allowed` remains false until a separately reviewed, version-specific pivot executor, credential transfer and post-move reconciliation workflow exist.

The EPH Agent must also find exactly one source `Cluster` object matching the requested deployment cluster name and return its namespace, UID and API version. Missing, ambiguous or incomplete source Cluster identity prevents the source preflight from passing.

## Operational failure handling

- If EPH fails before target provisioning completes, re-reserve or reboot the same physical machine into the RAM image, re-enroll a new EPH session, and resume from the central checkpoint after reconciling external state.
- If EPH disappears after handoff starts, do not automatically fail the target cluster. Wait for the expected target worker enrollment and reconcile by physical identity.
- If the target worker does not join before the deadline, mark reclaim as failed/pending recovery, retain the target cluster state, and use the out-of-band provisioning system to recover the physical node.
- If the same hardware appears with a different boot session, treat it as a new EPH runtime session, not as proof that the previous session is still alive.
- Never mark the node reclaimed solely because the EPH heartbeat stopped; require Target Agent confirmation that the expected physical machine is Ready in the target cluster.

## Implementation boundary

The current repository has EPH/Target agent enrollment, heartbeat, typed command dispatch, plan persistence, a Control-Plane-first Target Agent binding endpoint, and a central durable checkpoint read/write API with generation, monotonic sequence, idempotency and credential-field rejection; it also has read-only provider discovery and live CRD-backed schema validation. The target-Agent-on-Control-Plane deployment, durable task resume, CAPI bootstrap-cluster pivot, worker provisioning state machine, physical identity correlation, EPH handoff, credential revocation, OS re-image/reboot and same-machine worker re-enrollment are not implemented yet. CAPI/Metal3/Ironic mutation and the production provisioning adapter also remain disabled. This document defines the required behavior; it is not a claim that the complete lifecycle is already automated.
