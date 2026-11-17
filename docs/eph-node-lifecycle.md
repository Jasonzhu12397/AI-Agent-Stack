# EPH Single-Node and Target Agent lifecycle

## Authoritative topology

- EPH is one temporary, single-node Kubernetes cluster on one bootstrap host. It has one Kubernetes node and no EPH worker pool.
- The Target Kubernetes control plane is deployed first on its designated master/control-plane host(s).
- The Target Agent is deployed as a Pod on a Target Control Plane master. It restores the deployment checkpoint from the durable cloud service and independently owns the handoff.
- All BMH inventory and CAPI/Metal3 ownership required after bootstrap must be migrated and verified in the Target cluster before the EPH bootstrap environment is removed.
- The Target Agent provisions all Target Worker nodes after EPH cleanup.
- Chronyd is mandatory on EPH, Target Control Plane masters and Target Worker hosts. EPH uses an independent bootstrap NTP source before its kubeadm init. Target Control Plane nodes synchronize from approved external NTP sources and serve NTP internally on the Ironic internal IP/VIP. Every Worker synchronizes only from that internal Ironic IP/VIP, never from external NTP servers.

## Lifecycle

```text
EPH_SINGLE_NODE_READY
  -> TARGET_CONTROL_PLANE_READY
  -> TARGET_AGENT_READY
  -> CAPI_PIVOT_VERIFIED
  -> BMH_REGISTRY_MIGRATED
  -> EPH_CLEANUP_VERIFIED
  -> TARGET_WORKER_PROVISIONING
  -> TARGET_WORKERS_READY
  -> DEPLOYMENT_COMPLETED
```

Cloud Control Agent provides global scheduling, durable checkpoint storage, agent registry, command relay, audit and status visibility. It does not approve Target Agent adoption, BMH migration or EPH cleanup. These are autonomous Target Agent workflow transitions gated by machine-verifiable evidence.

## Mandatory gates

### Chrony and time synchronization

Before any kubeadm init/join operation, install the chrony package, deploy the role-specific chrony configuration, enable and start chronyd, verify source reachability, and require `chronyc tracking` to report synchronized state with acceptable leap status and offset. EPH uses its bootstrap NTP source. Target Control Plane hosts synchronize from external NTP sources and must expose an internal NTP service at the Ironic internal IP/VIP on UDP/123. Workers configure only that internal Ironic IP/VIP as their NTP source; external NTP and fallback pools are forbidden on Workers. A failed or unsynchronized clock blocks kubeadm.

### Target Control Plane

Require API reachability, healthy etcd, core networking, chronyd synchronization to external NTP and the internal Ironic NTP endpoint listening/reachable over UDP/123 before declaring the Control Plane ready for Worker provisioning or placing Target Agent.

### Target Agent adoption

Require the Target Agent Pod on a Target Control Plane master, successful cloud heartbeat, restored deployment/task checkpoint, matching deployment ID and preserved Target Cluster UID.

### CAPI pivot

Move the CAPI management controllers and Cluster API objects, not merely the Agent Pod. Before `clusterctl move`, record the source Cluster UID in the `ai-agent-stack.io/source-cluster-uid` annotation. The Target API Server assigns a new Kubernetes UID during migration, so never require the source and Target UIDs to be equal. Verify the migrated object name/namespace and source-UID annotation, record both UIDs, verify source absence, and then verify target controller availability, object ownership and reconciliation. A dry-run or simulated pivot is not a successful pivot.

### BMH registry migration

Require complete source and target inventories. Compare the exact set of (BareMetalHost name, UID), not just counts; Kubernetes UIDs are cluster-local diagnostics, not cross-cluster hardware identity. Verify the target Metal3/CAPM3 controller is healthy and owns/reconciles the migrated inventory. Do not remove source records before target ownership is confirmed.

### EPH cleanup

Only the Target Agent may initiate cleanup after Target Agent adoption, CAPI pivot and BMH migration are verified. Persist a checkpoint before and after each external side effect. Remove temporary EPH Agent/bootstrap Pods and EPH-only management components that no longer have dependencies; revoke EPH-only credentials. If any condition is unknown or fails, leave EPH intact and report a recoverable blocked state. Do not treat a lost EPH heartbeat as proof of cleanup success.

### Worker provisioning and chronyd

After EPH cleanup is verified, Target Agent drives CAPI/Metal3/Ironic Worker provisioning. Every Target Worker receives chronyd installation/configuration/enablement with only the Ironic internal IP/VIP as its NTP source. Verify UDP/123 reachability and that chronyd selected the expected internal source before kubeadm join. Worker completion requires matching CAPI Machine and Kubernetes Node identities, Ready conditions, chronyd verification and network postflight.

## Recovery and idempotency

- Persist deployment ID, Target Cluster UID, source/target BMH hardware identity inventory, phase, generation, command idempotency keys and operation results in durable cloud storage.
- On restart, Target Agent resumes from the last committed checkpoint and reconciles external state before retrying.
- A timeout after an external mutation is ambiguous; inspect live Kubernetes/CAPI/Metal3 state before retry.
- Never delete BMH source objects, remove EPH components, or advance worker provisioning based only on a timeout or missing heartbeat.
- A failure after EPH cleanup must be recoverable from the Target cluster without requiring the EPH API.

## Current implementation boundary

The `apps/node_agent/delivery_lifecycle.py` module implements topology validation, EPH/Target chronyd policy, hierarchical NTP source validation, exact BMH hardware identity-set comparison, and fail-closed lifecycle transition evidence checks. `apps/node_agent/chrony.py` adds a root-only host executor and CLI for chrony installation, managed configuration, service enablement, source selection and synchronization checks on Ubuntu/Debian and SLES/RHEL-family hosts. It binds the Target Control Plane NTP listener to the Ironic internal IP and rejects external/fallback sources on Workers. The host executor must be invoked by the host bootstrap/cloud-init integration; the current non-root Node Agent Pods cannot directly mutate host chronyd.

These policy primitives do not themselves execute clusterctl move, transfer Kubernetes objects, delete EPH Pods, revoke credentials, create MachineDeployments, or install chronyd. Those side effects require version-aware CAPI/Metal3/Kubernetes/host adapters and end-to-end integration tests. The implementation must not report a real handoff as complete until those adapters return verified postconditions.


**Identity note:** Kubernetes object UIDs are scoped to an API server and may change when an object is recreated or migrated to another cluster. They must not be the cross-cluster hardware identity key. Reconciliation compares namespace/name and stable hardware identity fields (boot MAC, BMC address, serial number and/or system UUID); Kubernetes UIDs are retained only as per-cluster diagnostics.
