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

### Provisioning defaults

The deployment form can prefill `ironic.networking.interface` and
`ironic.networking.ip_address` from the project provisioning network: the bond
name wins over the bare interface name, and the gateway IPv4 address is the
prefill default. Those values are editable, non-authoritative defaults - a real
lab may run Ironic on any host address inside the provisioning network, so the
operator remains responsible for the actual address. Image addresses stay
manual, because the registry mirrors differ per installation.

`packages/agent_contracts/ironic_spec.py:provisioning_defaults` derives the
defaults from the profile and `apply_provisioning_defaults` fills only missing
keys, reporting the filled keys in `spec.ironic_defaults_sources`. The platform
applies it inside `_validated_spec` before normalisation, so the EPH always
receives a complete payload while explicit operator values continue to win.
The web form exposes the same derivation plus a JSON template
(`{"ironic": {...}}`) that the operator can review before submitting, so the
configuration is generated from a template and pushed to the backend rather
than typed ad hoc.

### EPH host network

The EPH host is a physical machine, so its own bonds, VLANs, MTUs and routes
also come from the project network profile. `packages/agent_contracts/eph_network.py`
renders a single nmstate document (`eph_network_state`) from the planned
profile plus an explicit `eph_network_addresses` map (`role -> ip/prefix`).
Only addresses the operator owns are accepted; the control network (falling
back to provisioning) must be addressed unless it is DHCP, and when the Ironic
workload is prefilled the EPH-owned provisioning (or control) address wins over
the network gateway default, and secondary
segments require an address only when the operator supplies one. Same-link
untagged duplicates fail with `EPH_NETWORK_PRIMARY_SEGMENT_CONFLICT`, unknown
roles with `EPH_NETWORK_ADDRESS_ROLE_INVALID`, and malformed addresses with
`EPH_NETWORK_ADDRESS_INVALID`.

The rendered document is stored as `spec.eph_network` by the platform and
forwarded in every `eph.controlstack.apply` payload, where it is applied before
any control-stack manifest. The standalone `eph.network.configure` action
(`nmstatectl apply`, idempotent, declarative). A host without `nmstatectl` is
reported as `status=blocked` with `EPH_NETWORK_NMSTATE_UNAVAILABLE` instead of
half-applying the change, and a failed reconcile returns
`EPH_NETWORK_APPLY_FAILED` with the tool stderr.

SR-IOV PCI functions are not typed by hand. `observed_sriov_pci_defaults`
reads the collected host inventory (the same `nics` records produced by
`host.network.observe`, which PXE/discovery inventory feeds as well), matches
the declared interface or bond ports, and falls back to functions already
reported as `sriov_capable`. The platform applies it before planning, records
`spec.network_defaults.sriov_pci_sources` (origin, interfaces, PCI addresses)
and `SR-IOV_PCI_UNAVAILABLE:<network>` warnings when no function is known.
Explicit `pci_addresses` still win, so a lab with unusual topology can override
the inventory.

### Runtime preflight evidence

`eph.preflight` reports runtime findings in `output.warnings`. The control
plane stores that command result, and the platform mirrors it onto the
deployment through `POST /api/v1/cluster-deployments/<id>/preflight/eph/sync/`.
The endpoint reads the newest `eph.preflight` result from Cloud Control
(`GET /api/v1/control/deployments/<id>/preflight`), sanitises the warning list
(deduplicated, at most 32 entries of 160 characters) and stores it under
`preflight_result.eph_preflight` together with `status`, `error_code`,
`eph_ready`, `observed_at`, `command_id` and `synced_at`. Platform-side
conclusions such as `network_readiness` and `ironic_workload` are preserved by
the merge. An unavailable control plane returns 503/502 with
`eph_preflight_unavailable` instead of pretending the observation failed the
deployment, and the detail page shows both the stored snapshot and its sync
time so operators can tell a stale record from a fresh one.

The CR is applied separately from the operator manifest, only after
`kubectl wait --for condition=established crd/ironics.ironic.metal3.io`: a CR
cannot share a single server-side apply with the CRD that introduces it. If the
payload supplies both `spec.ironic` and an `Ironic` document in its manifest
source, the action refuses the ambiguity with `IRONIC_CR_MULTIPLE_SOURCES`.

CI validates the contract in two ways: unit tests pin the rendered document,
and `tests/integration/kind_metal3_crds.sh` installs the real `Ironic` CRD in a
kind cluster and server-side dry-runs the rendered CR.

## PXE discovery boot

`POST /api/v1/tenants/<tenant>/projects/<project>/fleet/machines/pxe-boot/` drives the Redfish operations that make discovered machines boot into the Ironic inspection image. The body accepts `execution` (boolean, default `false`), `confirm` (must equal `PXE_ONCE` when executing) and optional `machine_ids` (up to 100 UUIDs; the default is every machine with BMC endpoint and credential configured).

`apps/platform-api/core/bmc_pxe_boot.py` is the single implementation of the BMC side:

- `read_system` requires exactly one Redfish Systems member and a `/redfish/v1/Systems/<id>` path, so an ambiguous service root fails closed.
- `build_pxe_boot_plan` requires `Boot.BootSourceOverrideSupported` to contain `Pxe` (case insensitive) and a known `PowerState`; a powered-off host resets with `On`, anything else with `ForceRestart`.
- Execution first PATCHes `Boot.BootSourceOverrideTarget=Pxe` with `BootSourceOverrideEnabled=Once`, then POSTs `ComputerSystem.Reset`. A plan-only request performs GET requests and never mutates the BMC.
- BMC addresses must resolve to private addresses; the platform also honours `bmc_tls_verify` and the machine trust bundle.

Each machine is isolated: a failure records its error code (`PXE_BOOT_*`) without aborting the batch. The resulting plan or execution evidence is stored under `AgentMachine.hardware_inventory["pxe_boot"]` so the fleet view can show what was requested and when; it does not change `observed_at` or `inventory_revision`, because a boot override is not an inventory observation.

### Inspection ingestion

`metal3.host.inspect` is an EPH action: it runs `kubectl get bmh -A -o json` (or `-n <bmh_namespace>` when the payload names one) and parses the result through `packages/agent_contracts/metal3_bmh_inventory.py`. The parser is fail-closed: non-list input, a host without a name, duplicate namespace/name pairs, more than 256 hosts or more than 64 NICs per host are rejected with a `BMH_*` code. A missing `kubectl` yields `blocked/METAL3_BMH_KUBECTL_UNAVAILABLE`, a non-zero exit yields `failed/METAL3_BMH_INSPECT_FAILED`.

The EPH posts the resulting document to `POST /api/v1/tenants/<tenant>/projects/<project>/fleet/machines/bmh-inventory/` (administrator only). The platform matches machines by BMC host, then BMH name, then system UUID; unmatched hosts are reported instead of guessed. The BMH block is always stored as `hardware_inventory["bmh"]`. NIC records are adopted only when the BMH view carries PCI addresses and the machine has no PCI-bearing NIC inventory from the agent; only that adoption advances `inventory_source`, `inventory_revision` and `observed_at`, because those fields feed the 24-hour inventory gate and the SR-IOV PCI prefill.

The platform only performs the boot override. PXE itself belongs to the EPH Ironic deployment: the inspection image boots IPA, and `host.network.observe` (or `metal3.host.inspect`) refreshes `AgentMachine.hardware_inventory` with NIC names, addresses and SR-IOV PCI BDFs. Standard BMH `status.hardware` carries NIC names/MAC/model/speed only, so PCI addresses always come from the OS/IPA inventory, never from the Redfish plan.

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
