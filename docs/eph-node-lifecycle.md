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

The Target Agent moves the CAPI management controllers and Cluster API objects, not merely the Agent Pod. Before `clusterctl move`, record the source Cluster UID in the `ai-agent-stack.io/source-cluster-uid` annotation. The Target API Server assigns a new Kubernetes UID during migration, so never require the source and Target UIDs to be equal. Verify the migrated object name/namespace and source-UID annotation, record both UIDs, verify source absence, and then verify target controller availability, object ownership and reconciliation. A dry-run or simulated pivot is not a successful pivot.

### BMH registry migration

Require complete source and target inventories. Compare the exact set of (BareMetalHost name, UID), not just counts; Kubernetes UIDs are cluster-local diagnostics, not cross-cluster hardware identity. Verify the target Metal3/CAPM3 controller is healthy and owns/reconciles the migrated inventory. Do not remove source records before target ownership is confirmed.

### EPH cleanup

Only the Target Agent may initiate cleanup after Target Agent adoption, CAPI pivot and BMH migration are verified. Persist a checkpoint before and after each external side effect. Remove temporary EPH Agent/bootstrap Pods and EPH-only management components that no longer have dependencies; revoke EPH-only credentials. If any condition is unknown or fails, leave EPH intact and report a recoverable blocked state. Do not treat a lost EPH heartbeat as proof of cleanup success.

### Worker provisioning and chronyd

After EPH cleanup is verified, Target Agent drives CAPI/Metal3/Ironic Worker provisioning. Every Target Worker receives chronyd installation/configuration/enablement with only the Ironic internal IP/VIP as its NTP source. Verify UDP/123 reachability and that chronyd selected the expected internal source before kubeadm join. Worker completion requires matching CAPI Machine and Kubernetes Node identities, Ready conditions, chronyd verification and network postflight.

## Ironic workload CR

`eph.controlstack.apply` renders and applies the `Ironic` custom resource next
to the Ironic Standalone Operator. The CR is not optional: the control stack
readiness probe requires the `Ironic` resource `Ready` condition, so a stack
without it would block until `CONTROL_STACK_READY_TIMEOUT:ironic`. The action
therefore fails fast with `IRONIC_WORKLOAD_CR_REQUIRED` when neither
`spec.ironic` nor an `Ironic` document in the supplied manifests is present.

All environment-specific values are required from the deployment spec:

| Key | Required | Maps to |
| --- | --- | --- |
| `ironic.api_credentials_name` | yes | `spec.apiCredentialsName` |
| `ironic.version` | yes | `spec.version` |
| `ironic.images.ironic` | yes | `spec.images.ironic` |
| `ironic.images.deploy_ramdisk_downloader` | yes | `spec.images.deployRamdiskDownloader` |
| `ironic.images.keepalived` | yes | `spec.images.keepalived` |
| `ironic.networking.interface` | yes | `spec.networking.interface` |
| `ironic.networking.ip_address` | yes | `spec.networking.ipAddress` |
| `ironic.networking.{api_port,rpc_port,image_server_port,image_server_tls_port}` | no | ports |
| `ironic.networking.{external_ip,external_callback_url,image_server_external_url,ip_address_manager}` | no | networking strings |
| `ironic.networking.{mac_addresses,disable_host_network,bind_interface}` | no | list / booleans |

The namespace is fixed to `baremetal-operator-system` because that is where the
readiness probe looks; any other value is rejected
(`IRONIC_NAMESPACE_UNSUPPORTED`). The renderer never fills in a missing value:
each absent required key produces its own error code
(`IRONIC_CREDENTIALS_REQUIRED`, `IRONIC_VERSION_REQUIRED`,
`IRONIC_IMAGE_REQUIRED`, `IRONIC_INTERFACE_REQUIRED`,
`IRONIC_IP_ADDRESS_REQUIRED`) and the action returns `status=failed` without
touching the cluster.

The rules live once in `packages/agent_contracts/ironic_spec.py`; the EPH
renderer, the platform validator and the deployment form all consume the same
contract instead of re-implementing it. The platform validates the contract at
deployment creation: an invalid `cluster_spec.ironic` returns HTTP 400 with the
normaliser's error code
(`ironic: IRONIC_IMAGE_REQUIRED`), before any fleet dispatch. The dispatch
payload always re-injects `ironic` from the deployment spec, because a cached
`plan_result.cloud_control.spec` may predate it. The orchestration contract
forwards `ironic` as a mapping in the `eph.controlstack.apply` payload, and
`eph.preflight` reports `IRONIC_WORKLOAD_CR_REQUIRED` or the renderer code in
`output.warnings` so the gap surfaces before the node is bootstrapped.

The CR is applied separately from the operator manifest, only after
`kubectl wait --for condition=established crd/ironics.ironic.metal3.io`: a CR
cannot share a single server-side apply with the CRD that introduces it. If the
payload supplies both `spec.ironic` and an `Ironic` document in its manifest
source, the action refuses the ambiguity with `IRONIC_CR_MULTIPLE_SOURCES`.

CI validates the contract in two ways: unit tests pin the rendered document,
and `tests/integration/kind_metal3_crds.sh` installs the real `Ironic` CRD in a
kind cluster and server-side dry-runs the rendered CR.

## Recovery and idempotency

- Persist deployment ID, Target Cluster UID, source/target BMH hardware identity inventory, phase, generation, command idempotency keys and operation results in durable cloud storage.
- On restart, Target Agent resumes from the last committed checkpoint and reconciles external state before retrying.
- A timeout after an external mutation is ambiguous; inspect live Kubernetes/CAPI/Metal3 state before retry.
- Never delete BMH source objects, remove EPH components, or advance worker provisioning based only on a timeout or missing heartbeat.
- A failure after EPH cleanup must be recoverable from the Target cluster without requiring the EPH API.

## Current implementation boundary

The Target Agent `apps/target_agent/capi_pivot.py` adapter performs source/target API separation checks, requires three Ready Target masters, verifies that all listed CAPI/Metal3/Ironic provider controllers are Ready and scheduled on those masters, checks that target provider versions are not older than source, runs `clusterctl move --dry-run`, and permits the real move only when the command approval digest matches the approved plan digest. It annotates the source Cluster with its source UID and stable logical identity before the move, then verifies the target annotation, source-object absence, Target controller readiness and exact BMH hardware-identity set. The source and target Kubernetes object UIDs are recorded separately; equality is not required.

The new `apps/target_agent/handoff.py` module implements ordered, fail-closed phase and evidence validation. `apps/target_agent/ntp_policy.py` validates that each Worker has exactly the Ironic internal IP/VIP as its only NTP source and renders a chronyd pre-kubeadm command that installs chrony, replaces the source list, enables the service and verifies the selected source. Control Plane and Worker chrony bootstrap commands are generated but are not yet injected into CAPI KubeadmConfig templates or executed on physical hosts.

The Target Agent persists validated handoff checkpoints through the Cloud Control API using an active command lease and compare-and-swap sequence/phase checks. The Target Pivot adapter and Target handoff state machine are implemented but have not yet been validated against live provider versions. Autonomous EPH deletion/credential revocation, Worker provisioning and Target Control Plane NTP serving still require new adapters and end-to-end integration tests. The implementation must not report a real handoff as complete until those adapters return verified postconditions.


**Identity note:** Kubernetes object UIDs are scoped to an API server and may change when an object is recreated or migrated to another cluster. They must not be the cross-cluster hardware identity key. Reconciliation compares namespace/name and stable hardware identity fields (boot MAC, BMC address, serial number and/or system UUID); Kubernetes UIDs are retained only as per-cluster diagnostics.
