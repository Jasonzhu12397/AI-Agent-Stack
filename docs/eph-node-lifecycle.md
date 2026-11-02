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
  -> approved provisioning of the target cluster
  -> target control plane and required worker capacity become Ready
  -> bootstrap Target Agent in the target cluster
  -> Target Agent confirms target-side health and worker readiness
  -> persist handoff intent and final-node bootstrap configuration centrally
  -> cordon/drain or otherwise quiesce the temporary EPH Kubernetes workload
  -> stop temporary EPH services and invalidate EPH runtime credentials
  -> reboot/re-network-boot the same physical node into the target worker OS
  -> join the target cluster as a worker using a short-lived join credential
  -> Target Agent verifies the expected physical node is Ready
  -> mark the physical node reclaimed and return it to target compute capacity
```

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
9. **CAPI/Metal3 state ownership:** if CAPI/Metal3 controllers or their management objects are hosted inside the temporary EPH Kubernetes cluster, that state disappears at reclamation. Therefore all required provisioning must finish before handoff, and any state needed for later day-2 management must be transferred to a durable management plane or represented by the central platform. The implementation must not claim that an ephemeral CAPI management cluster provides persistent day-2 reconciliation.
10. **Idempotent reclaim:** handoff, EPH credential revocation, target worker enrollment, and completion reporting must be idempotent and bound to the same deployment ID and physical inventory identity.

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
  -> TARGET_AGENT_READY
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

## Operational failure handling

- If EPH fails before target provisioning completes, re-reserve or reboot the same physical machine into the RAM image, re-enroll a new EPH session, and resume from the central checkpoint after reconciling external state.
- If EPH disappears after handoff starts, do not automatically fail the target cluster. Wait for the expected target worker enrollment and reconcile by physical identity.
- If the target worker does not join before the deadline, mark reclaim as failed/pending recovery, retain the target cluster state, and use the out-of-band provisioning system to recover the physical node.
- If the same hardware appears with a different boot session, treat it as a new EPH runtime session, not as proof that the previous session is still alive.
- Never mark the node reclaimed solely because the EPH heartbeat stopped; require Target Agent confirmation that the expected physical machine is Ready in the target cluster.

## Implementation boundary

The current repository has EPH/Target agent enrollment, heartbeat, typed command dispatch, plan persistence, read-only provider discovery and live CRD-backed schema validation. The full EPH RAM boot lifecycle, physical inventory correlation, handoff state machine, OS re-image/reboot orchestration, credential revocation at handoff, and automatic re-enrollment of the same machine as a target worker are not implemented yet. CAPI/Metal3/Ironic mutation and the production provisioning adapter also remain disabled. This document defines the required behavior; it is not a claim that the complete lifecycle is already automated.
