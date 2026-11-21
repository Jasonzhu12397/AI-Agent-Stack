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

A network may set `default_route: true` in the profile. Only one network may
declare it (`INFRA_NETWORK_DEFAULT_ROUTE_DUPLICATE`), it must have a gateway
(`INFRA_NETWORK_DEFAULT_ROUTE_GATEWAY_REQUIRED`) and it cannot be an SR-IOV
network. The rendered document then routes `0.0.0.0/0` through that network's
interface instead of the control link, which is what the lab reference does
(`ccd_om` VLAN 3200 carries the default route, `sdi_semc` VLAN 70 carries a
segment route and `bond_control` stays untagged). Without that flag the default
route falls back to the control/provisioning gateway. A VLAN network may omit
both `bond` and `interface`: it then inherits its base link from its own data
bond or from the primary link, so a storage VLAN no longer has to repeat the
data bond definition.

The Metal3 `networkData` document (`render_role_network_data`) now follows the
same two rules: only the declared default-route network carries `0.0.0.0/0`
(falling back to the control gateway when nothing is declared), other addressed
segments carry a route to their own CIDR, and VLAN networks are rendered as
`vlans` links with `vlanId` and an explicit parent. The OS contract
(`derive_network_spec`) names the link that actually holds the address, so a
VLAN profile reports `bond_data.3300` rather than the bare bond; cloud-init and
postflight compare the same name on both sides.

`tests/test_eph_network_reference_parity.py` pins the rendered document to the
uploaded lab reference: untagged control address, VLAN 70 on `bond_control`,
802.3ad `bond_data` with both ports and MTU 9000, VLANs 3220/3200 on
`bond_data`, the default route on VLAN 3200 and the segment route on VLAN 70.

The readiness report also exposes `secret_presence` (`configured` / `empty` per
kubeconfig secret). It reports configuration state only and never echoes the
kubeconfig content.

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

The EPH already reports the action result through the command lease channel, so operators do not have to paste JSON. The control plane exposes `GET /api/v1/control/deployments/<id>/actions/<action>/result`, returning the newest command result for one action (unknown or malformed action names are rejected, no other deployment is read). The platform mirrors that result with `POST /api/v1/cluster-deployments/<id>/bmh-inventory/sync/`: the command status must be `succeeded` before any host is trusted, and the merged snapshot is stored under `preflight_result["bmh_inventory_sync"]` for auditability.

The same document can still be submitted manually to `POST /api/v1/tenants/<tenant>/projects/<project>/fleet/machines/bmh-inventory/` (administrator only). The platform matches machines by BMC host, then BMH name, then system UUID; unmatched hosts are reported instead of guessed. The BMH block is always stored as `hardware_inventory["bmh"]`. NIC records are adopted only when the BMH view carries PCI addresses and the machine has no PCI-bearing NIC inventory from the agent; only that adoption advances `inventory_source`, `inventory_revision` and `observed_at`, because those fields feed the 24-hour inventory gate and the SR-IOV PCI prefill.

### Fleet refresh applies reported inspections

`core/fleet_inspection.py` reads the ``metal3.host.inspect`` results that are
already stored on succeeded ``AgentCommand`` records and applies them through
the same ``normalize_hosts`` / ``match_machines`` / ``merge_bmh_host`` path as
the manual ingest endpoint:

- `GET .../fleet/machines/` runs a best-effort pass first, so a discovery chain's
  PCI inventory appears on the next fleet refresh without opening a deployment
  or pressing a sync button;
- each machine records the applied command id (``bmh_inspect_command_id``), so a
  repeated pass does not replay the same observation or advance
  ``inventory_revision``;
- a failed command, a malformed result, or a machine that already has
  agent-observed PCI is left untouched: the weaker BMH view never replaces an
  Agent observation.

### Discovery chain: one confirmed action

`POST /api/v1/tenants/<tenant>/projects/<project>/fleet/machines/discovery/`
composes what used to be three manual steps — plan, execute, ingest — into one:

1. every machine with BMC configuration gets the one-time Redfish `Pxe` override
   and reset (`_pxe_boot_results`, the same evidence written to
   `hardware_inventory["pxe_boot"]`);
2. only when at least one machine actually booted, `metal3.host.inspect` is
   queued on every online EPH node of the project.

Guarantees: execution needs `confirm="PXE_ONCE"` and an `Idempotency-Key`
(repeating the key returns `already_queued` instead of a second command), a
failed boot is never counted as discovered (`NO_MACHINE_BOOTED`), an offline EPH
is reported as `EPH_OFFLINE` instead of being skipped silently, and a plan-only
call (`execution: false`) never queues an inspection. The Fleet Inventory
toolbar exposes it as "Run discovery (PXE + inspect)".

### Snapshot reconciliation on read

The EPH reports results only through the command lease channel. The platform
used to require two manual sync buttons before ``preflight_result`` knew
anything, so it normally rendered from the spec instead of from observations.
``apps/platform-api/core/deployment_reconcile.py`` now owns both transforms
(``eph_preflight_snapshot`` / ``bmh_inventory_snapshot``) and one
``reconcile_deployment`` entry point:

- ``GET /api/v1/cluster-deployments/<id>/`` reconciles best-effort before
  rendering, so a snapshot appears without a click; a bridge outage returns the
  page unchanged instead of an error.
- A snapshot is written only when the EPH observation changed
  (``command_id`` / ``observed_at``), so repeated reads do not churn
  ``updated_at`` or machine inventory.
- A failed or incomplete ``metal3.host.inspect`` is never trusted: no machine
  is touched and the failure is recorded only when the operator explicitly
  forces a sync.
- ``GET /api/v1/control/deployments/<id>/preflight`` was a duplicate of
  ``/actions/eph.preflight/result`` specialised to one action; it has been
  removed, and the platform client reads the action channel for both snapshots.

### Ironic template is rendered by the platform

`packages/agent_contracts/ironic_template.py` is the only place that answers
"what does this provisioning network imply for Ironic". It returns the
configuration skeleton, the keys it filled (`defaults_sources`), the fields the
operator still owns (`manual_fields`: credentials name, version and the three
image URLs) and blocking warnings
(`IRONIC_TEMPLATE_PROVISIONING_NETWORK_MISSING`, `_INTERFACE_UNAVAILABLE`,
`_ADDRESS_UNAVAILABLE`).

- Image URLs are never defaulted: registry and tag choices belong to the
  operator, so the template leaves them empty and names them instead of
  inventing a mirror.
- The networking link goes through `network_link_id`, so a VLAN provisioning
  network binds `bond-prov.400` / `eno4.400` — the same sub-interface the
  nmstate renderer and Metal3 `networkData` name — instead of the bare bond.
- An EPH-owned address beats the profile gateway as the Ironic address.
- `POST /api/v1/tenants/<tenant>/projects/<project>/cluster-deployments/ironic-template/`
  exposes the same renderer, and the UI previews it instead of re-deriving the
  mapping in TypeScript (`provisioningNetworkDefaults` was removed).
- The deployment validator uses the same `provisioning_networking` call, and
  appends its warnings to `preflight_result.network_defaults.warnings`, so the
  detail page shows the same gaps the template reports.

### Single topology resolver and command registry

Two classes of duplication used to let the same decision live in two places:

- Link naming, VLAN parents and default-route ownership were implemented once
  in `infra_networks.py` and again in `eph_network.py`. They now live in
  `network_link_id` / `vlan_base_link` only; the nmstate renderer imports them,
  so a VLAN profile cannot address `bond_data` in one document and
  `bond_data.3300` in the other. `tests/test_eph_network_reference_parity.py`
  asserts both documents name the same links for a VLAN profile.
- The Agent command surface was restated in the platform next to the
  orchestration sets, which already caused `metal3.host.inspect` to exist in
  only one of them. `packages/agent_contracts/agent_commands.py` is now the
  single registry (`EPH_AGENT_COMMANDS` / `TARGET_AGENT_COMMANDS` =
  direct commands ∪ orchestration actions), the platform imports it, and
  `tests/test_agent_command_registry.py` asserts that every orchestration step
  is dispatchable, every Agent handler is authorized, and every orchestration
  step has a handler. Approval policy (`MUTATING_COMMANDS`) stays platform-local
  because it is not an Agent capability.

### Lab workflow tag in the UI

`GET /api/v1/metal3/lab-workflow/` parses `.github/workflows/metal3-lab-e2e.yml`
(`core/lab_workflow.py`) and reports only what the checked-in file proves: the
acceptance job's `runs-on` labels (`self-hosted`, `metal3-lab`, `physical`), its
`needs` + `ready == 'true'` gate, the declared `workflow_dispatch` inputs, and
the secret names the workflow consumes. The Fleet Inventory panel renders it as
a tag plus the individual checks.

- `secret_presence` is always `not_observable`: the platform cannot read
  GitHub repository secrets, so the tag must not turn green in a way that
  implies a lab run happened. Presence is decided by the Lab readiness job, whose
  report also lands in the workflow artifacts.
- The tag appears both on the Fleet Inventory panel and on the Overview
  deployment pipeline, so the lab precondition is visible before a deployment
  is planned.
- `METAL3_LAB_WORKFLOW_URL` adds an "Open workflow" link when the deployment
  sets it; the tag itself never calls GitHub.
- PyYAML resolves the unquoted `on:` key to boolean `True`, so the trigger block
  is looked up under both spellings; a naive lookup silently loses the dispatch
  inputs.

### Lab workflow contract

The physical acceptance workflow (`.github/workflows/metal3-lab-e2e.yml`) can only run on the `self-hosted, metal3-lab, physical` runner, so it stays manual and gated behind a readiness report. `.github/workflows/metal3-lab-contract.yml` runs on every change to the lab workflow, the readiness/acceptance tools or their tests, and proves two things on a GitHub-hosted runner: the workflow YAML still satisfies its contract, and the readiness gate keeps its honest semantics — missing kubeconfig secrets must report `ready=false` with the failed checks listed, a well-formed kubeconfig must report `ready=true`. A green contract run is not evidence of a bare-metal deployment; it only proves the pipeline cannot silently claim one.

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

## Machine registration and lifecycle in the UI

Every machine-level action addresses an `AgentMachine`, so the fleet inventory row
carries `machine_id`, `bmc_configured`, `bmc_host` (hostname of the Redfish
endpoint only, never the credential or trust references), `bmh_name`,
`inventory_revision`, `lifecycle_state` and `lifecycle_targets`.

- **BMC registration** (`PUT .../fleet/machines/<machine_id>/bmc-config/`) is the
  first step for a machine that has never been inspected. Without an endpoint and
  a credential reference the platform cannot read Redfish, so PXE boot, discovery
  and firmware management all fail closed. The endpoint must be private-network
  `https`. The dialog in Fleet Inventory sends only `endpoint`,
  `credential_ref` and an optional `trust_bundle_ref`; the credential value itself
  is resolved at dispatch time and is never stored by the platform.
- **Lifecycle** is a contract, not a view detail:
  `packages/agent_contracts/machine_lifecycle.py` defines
  `discovered → identity_review`, `identity_review → commissioned | discovered`,
  `commissioned → retired`, `retired → discovered`. The review endpoint rejects
  anything the contract does not allow with `LIFECYCLE_TRANSITION_NOT_ENABLED`,
  and the API returns `lifecycle_targets` so the UI never duplicates the table.
  Reviews require a 10–500 character reason and are audited as
  `fleet.machine.lifecycle_reviewed`.
- **The lifecycle gates provisioning.** PXE boot and discovery only accept
  machines in `discovered`, `identity_review` or `commissioned`. A `retired`
  machine must be returned to `discovered` first, otherwise both endpoints
  answer `409 MACHINE_NOT_BOOTABLE` with the offending machines and their
  states before any Redfish call is made; an unknown state fails closed the
  same way. The fleet-wide selection leaves non-bootable machines out instead of
  silently skipping them.

## Package layout

`agent_contracts` lives only in `packages/agent_contracts`. It previously existed
twice (`apps/agent_contracts` held the SR-IOV device-plugin contract), so a plain
`import agent_contracts` resolved to a different module depending on `sys.path`
ordering — tests and containers could load different contract code. The SR-IOV
module now lives in `packages/agent_contracts/sriov_device_plugin.py`, the
duplicate package and its `COPY apps/agent_contracts` Dockerfile lines are
removed, and importers use the canonical package name.
