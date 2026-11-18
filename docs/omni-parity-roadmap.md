# Omni-Inspired Bare-Metal Kubernetes Lifecycle Roadmap

## Architecture boundary

The product uses an Omni-inspired cluster lifecycle and fleet-management model, but its current infrastructure execution plane is **Cluster API (CAPI) + Metal3 + Ironic**. There is no direct Sidero Omni API client in this repository. Do not describe the current implementation as an Omni integration.

The supported runtime architecture is three agents:

- **Cloud Control Agent** runs in the cloud and manages deployment intent, orchestration, policy, budgets, and status.
- **EPH Agent** runs temporarily in a single-node EPH Kubernetes bootstrap cluster. It is not deployed once per physical server.
- **Target Agent** runs on the target Kubernetes control-plane/master nodes. It owns CAPI pivot, target-side BMH handoff, EPH runtime cleanup, and worker provisioning.

There is no active Node Agent runtime. Target takeover is an automatic Target Agent responsibility after target control-plane readiness and evidence checks; Cloud Control does not approve or review the takeover.

## Intended deployment sequence

1. Cloud Control creates a deployment intent and starts the EPH bootstrap workflow.
2. EPH Agent operates the single-node EPH cluster and uses CAPI, Metal3, and Ironic to provision the target Kubernetes control plane.
3. Target Agent starts on the target control-plane nodes after the target control plane is ready. It obtains target-cluster credentials from its in-cluster ServiceAccount and reads the source-cluster operator kubeconfig from its dedicated Secret.
4. Target Agent verifies source/target identity, CAPI provider readiness, and BMH ownership evidence, then performs the CAPI move.
5. Before changing source inventory, Target Agent validates the target observer Secret identity, deployment ownership, credential scope, source UID, and kubeconfig bytes. It then attests the cleanup inventory and performs the source-side ownership updates.
6. Target Agent cleans up the temporary EPH runtime and manages the BMH registration/ownership handoff in the target cluster.
7. Target Agent provisions worker MachineDeployments through CAPI/Metal3. Workers use the internal IP of the target control-plane Ironic endpoint for NTP; control-plane nodes use the configured external NTP sources.

## Implemented in the current codebase

The following are implemented in code and covered by the current CI suite:

- EPH CAPI/Metal3 rendering and guarded server-side dry-run/apply paths.
- Target-side CAPI pivot with source and target kubeconfig handling, provider readiness checks, and ownership evidence.
- EPH cleanup inventory validation, including Metal3 namespace Role/RoleBinding resources and target observer Secret checks before mutation.
- Target Agent worker provisioning using CAPI/Metal3 resources, Ironic capacity checks, and the internal Ironic NTP endpoint.
- Worker network-contract validation for control/data-plane separation, Multus NetworkAttachmentDefinitions, OVS/SR-IOV CNI types, and SR-IOV resource-name alignment.
- SR-IOV Device Plugin ConfigMap ownership and resource validation, plus checks that the existing Device Plugin DaemonSet mounts the expected ConfigMap and matches the configured pool node selectors and tolerations.
- A separately configurable SR-IOV Device Plugin namespace with scoped ConfigMap RBAC.
- Tenant token reservation, idempotency, expiry, daily/monthly budget enforcement, and bounded model/tool outputs.
- Three-agent deployment manifests, Agent mTLS ingress validation, Vue production build, Compose startup, and API/UI smoke tests.

The current main-branch CI run is the automated verification baseline. CI does not replace a physical bare-metal acceptance test.

## Remaining implementation work

### 1. Network add-on lifecycle

Worker provisioning currently **verifies** that the required NetworkAttachmentDefinitions and SR-IOV Device Plugin DaemonSet already exist and match the requested resource, node-selector, and toleration contract. It does not yet install or upgrade the full network stack.

The next implementation must add a versioned network-addon release adapter for:

- Multus CNI
- OVS CNI and OVS datapath configuration
- Calico control-plane/pod networking
- SR-IOV CNI
- SR-IOV Network Device Plugin and its per-pool scheduling policies
- DPDK/VF binding and resource exposure

The adapter must pin images/charts, validate rendered manifests, use server-side dry-run before apply, record ownership, and support rollback. It must not silently continue when a required NAD, resource name, DaemonSet, or VF is missing.

### 2. Day-2 lifecycle

The current worker path fails closed on unsafe scale-down. A drain executor and disruption-aware workflow are still required before enabling scale-down. Upgrade, repair, replacement, and rollback workflows also need complete state machines and end-to-end tests.

### 3. Physical infrastructure acceptance

Run acceptance tests against actual BMC/Redfish or IPMI endpoints, Ironic inspection and deployment, IPA boot media lifecycle, CAPI pivot, BMH ownership transfer, EPH cleanup, chrony reachability, SR-IOV VF allocation, and DPDK dataplane traffic. Unit tests and Compose smoke tests cannot prove these hardware/network behaviors.

### 4. Omni-style product capabilities

The product still needs complete fleet inventory, cluster lifecycle views, day-2 operations, multi-provider addon management, and full GUI/backend parity. If direct integration with an external Omni service is required, implement it as a separate provider adapter; it is not part of the current CAPI/Metal3 execution path.

## Safety invariants

- Never reintroduce the deprecated Node Agent runtime.
- Never allow Cloud Control to approve or gate Target Agent takeover.
- Never mutate source inventory before validating target identity and cleanup ownership.
- Never provision workers when required Ironic capacity, internal NTP, NADs, SR-IOV resources, or Device Plugin scheduling policy is missing.
- Never enable scale-down until a drain executor and disruption checks are present.
- Never treat CI success as proof of physical bare-metal or DPDK dataplane acceptance.
