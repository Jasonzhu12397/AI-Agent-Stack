# Metal3/Ironic Community Evidence and Validation Profile

## Purpose

This profile records upstream implementation and test evidence that the AI Agent Stack must follow before enabling real bare-metal provisioning. It is an engineering input, not a claim that this repository has deployed hardware.

## Upstream sources inspected

| Project | Source | Evidence used |
|---|---|---|
| CAPM3 | https://github.com/metal3-io/cluster-api-provider-metal3/blob/main/docs/deployment_workflow.md | Controller prerequisites, BMH-to-Metal3Machine association, data secret generation, provisioning and providerID handoff |
| CAPM3 | https://github.com/metal3-io/cluster-api-provider-metal3/blob/main/docs/getting-started.md | Ironic/BMO/BMH prerequisites, clusterctl lifecycle and pivot/Ironic ordering |
| CAPM3 | https://github.com/metal3-io/cluster-api-provider-metal3/blob/main/docs/e2e-test.md | Integration/basic/feature/upgrade suites, pivot sequence, test filters and environment requirements |
| CAPM3 | https://github.com/metal3-io/cluster-api-provider-metal3/blob/main/test/e2e/pivoting.go | Actual pivot/re-pivot test structure, controller/Ironic movement, API stability, BMH/M3Machine/Machine postconditions and artifact collection |
| BMO | https://github.com/metal3-io/baremetal-operator/blob/main/test/e2e/README.md | Libvirt BMH VMs, VBMC/sushy-tools, Ironic host networking, Redfish/IPMI paths, required provisioning/deprovisioning tests |
| BMO | https://github.com/metal3-io/baremetal-operator/blob/main/docs/dev-setup.md | Real Ironic local deployment versus fixture provisioner distinction |
| Metal3 dev env | https://github.com/metal3-io/metal3-dev-env | Reproducible end-to-end environment and component test matrix |

Upstream references are living documents. The adapter must pin immutable release tags/commits and record the exact versions used by each deployment; it must not silently follow `main`.

## Critical distinction: fixture is not Ironic

BMO explicitly supports a fixture provisioner for tests where Ironic is unavailable. The upstream E2E guide says the fixture path skips VMs, BMC emulators and Ironic. The fixture path is useful for controller/API contract tests only. It cannot be reported as successful BMC inspection, power control, IPA boot, image deployment, disk cleaning, detach or physical deprovisioning.

The real BMO E2E path uses libvirt VMs as BMHs, VBMC or sushy-tools as BMC emulators, and Ironic reachable on the provisioning network. The suite is designed to be configurable for real hardware through a BMC configuration file. Our test reports must label these levels separately:

- `UNIT`: pure validation and state-machine tests.
- `FAKE_API`: Kubernetes API/CRD contract tests without controllers or Ironic.
- `BMO_FIXTURE`: BMO fixture provisioner; never evidence of Ironic.
- `LIBVIRT_IRONIC`: Ironic plus IPA and emulated BMC/physical VM lifecycle.
- `REAL_HARDWARE`: actual BMC, provisioning network and physical host lifecycle.

Only the last two can verify the Ironic/IPA provisioning path; only `REAL_HARDWARE` verifies the user's actual BMC/network/hardware combination.

## Required controller and resource flow

1. Verify the management Kubernetes API and exact installed CRDs/schemas.
2. Verify compatible CAPI core, kubeadm bootstrap, kubeadm control-plane, CAPM3, BMO and Ironic/IrSO controller versions.
3. Verify Ironic is running and reachable on the provisioning network; verify IPA kernel/initramfs/image URLs and checksums.
4. Discover BMH inventory. Each selected BMH must be in `ready`/available state, have a usable BMC credential reference, stable provisioning-network connectivity, inspected hardware data and a valid root-device selection.
5. Create/validate Cluster, Metal3Cluster, KubeadmControlPlane, MachineDeployment, KubeadmConfigTemplate, Metal3MachineTemplate and any Metal3DataTemplate/secret references as a single versioned plan.
6. CAPM3 associates Metal3Machine with a BMH. When data templates are used, Metal3DataClaim/Metal3Data render metadata/network-data secrets before BMH provisioning starts.
7. BMO/Ironic performs inspection and deployment. Observe BMH state transitions and controller events; do not equate object creation with deployment.
8. After the host boots, correlate the workload Node to the BMH through the CAPM3 providerID workflow. Current upstream documentation describes the default providerID as `metal3://<namespace>/<bmh-name>/<m3m-name>`; the legacy BMH-UUID-only format is deprecated in newer CAPM3 releases.
9. Require BMH `provisioned`, Metal3Machine `Ready`, CAPI Machine `Running`, and a workload Node with matching providerID/identity and `Ready` condition before declaring the machine delivered.
10. Capture source/target manifests, controller logs, Ironic logs, IPA logs, events and clusterctl move logs as immutable test artifacts.

## Pivot requirements

CAPM3's E2E documentation treats pivot as an ordered lifecycle, not as moving only an Agent Pod. Before `clusterctl move`:

- Target cluster API must be stable and reachable.
- Required CAPI/CAPM3 controllers and Ironic/BMO components must be installed in the target as required by the selected topology.
- Ironic must not run duplicate DHCP/provisioning services in source and target simultaneously. Upstream recommends scaling down/removing the source Ironic instance before enabling it on the target.
- Provisioning-network nodes should use stable/static IP configuration; DHCP conflicts and Keepalived assumptions must be checked.
- CRDs required for move must carry the appropriate clusterctl move labels.
- Capture logs/manifests before and after move.
- After move, wait for Cluster, control plane, BMH, Metal3Machine and Machine postconditions; then test re-pivot where applicable.

The implementation must make the pivot scope explicit, preserve owner references and UIDs, and stop on missing CRDs, unstable API servers, duplicate Ironic/DHCP risk, or incomplete postconditions. A successful `clusterctl move --dry-run` is not a pivot.

## EPH reclaim safety contract

EPH is a temporary RAM-booted single-node Kubernetes host and is reclaimed as a target Data Plane worker. Its reclaim must be a separate state machine from CAPI pivot:

1. Target Control Plane is healthy and Target Agent is deployed on a target control-plane node.
2. Target Agent resumes the same durable deployment/task from the central Supervisor and reports healthy heartbeat.
3. CAPI/CAPM3 controllers and all required Cluster API objects have been moved and verified reconciling in the target management location. No required controller, webhook, secret, kubeconfig or durable state remains only on EPH.
4. Worker provisioning is complete and the target cluster is healthy without EPH.
5. EPH is cordoned/drained only after identity and workload policy checks; system-critical and local-storage workloads must be explicitly handled. Do not use blanket force deletion or delete-local-data.
6. Verify no protected workload, volume, local PV, CAPI controller, webhook, or management dependency remains on EPH. Revoke EPH credentials and mark the temporary cluster disposable only after the central checkpoint is durable.
7. Re-image/reboot EPH using the approved worker image and Data Plane network profile.
8. Target Agent matches the returning Kubernetes Node to the reserved physical host using a stable hardware identity (not hostname alone), then verifies Node Ready, expected labels/taints, CNI attachments and Data Plane network postconditions.
9. Mark EPH reclaimed only after all checks pass. On timeout, identity mismatch, missing node, or failed network checks, remain fail-closed and require operator recovery; never infer success from EPH heartbeat loss.

## Split Control Plane / Data Plane network checks

The user's target topology is authoritative: node management and Kubernetes API traffic use the ordinary Control Plane NIC; Pod-originated traffic defaults to the Data Plane. Calico is the primary CNI; Multus attaches secondary OVS and/or SR-IOV networks. DPDK readiness additionally requires verified VFIO/IOMMU, device plugin resources, hugepages, CPU/NUMA alignment and driver binding.

The plan and postflight must record explicit interfaces, VLANs, CIDRs, gateways, MTUs, IPAM, routes, CNI/plugin versions and SR-IOV resource names. No interface name, MTU, CIDR or gateway may be guessed. A successful Multus network-status annotation check is attachment metadata only; it is not proof of routing or packet forwarding. Add route-table and reachability tests for both workload Pods and the Agent Pod, and verify that Data Plane loss does not sever node-originated cloud-agent heartbeat or Kubernetes API management access.

## Implementation gates

| Gate | Required evidence | Failure behavior |
|---|---|---|
| Provider compatibility | Pinned versions, served CRD versions, OpenAPI schemas, controller image digests and supported matrix | Reject plan |
| BMH readiness | Ready/available state, inspection result, BMC credential reference, root-device and network checks | Exclude host |
| Ironic readiness | API health, provisioning network, IPA assets/checksums, no duplicate DHCP/Ironic | Reject provisioning |
| Manifest validation | Local schema validation plus Kubernetes server-side dry-run and admission/webhook result | Reject; no writes |
| Provisioning | Observed BMH/Ironic/IPA transitions and CAPI postconditions | Pause/reconcile; do not mark success |
| Pivot | Stable APIs, correct move labels, Ironic handoff, target controller reconciliation | Keep source intact; no EPH reclaim |
| EPH reclaim | Drain/dependency checks, durable checkpoint, re-image, stable hardware identity, Ready worker and Data Plane verification | Fail closed |

## Test execution plan

1. Run UNIT and FAKE_API tests on every change.
2. Run BMO_FIXTURE tests for object/state-machine contract coverage, clearly labeled as fixture.
3. Run LIBVIRT_IRONIC using the upstream metal3-dev-env/BMO E2E setup. Include inspection, provisioning, detach/deprovision, automated cleaning, pivot and re-pivot.
4. Run REAL_HARDWARE only with an explicitly supplied BMC inventory/configuration and isolated provisioning network. Record hardware model, BMC protocol/firmware, BIOS settings, NIC/MTU/VLAN, IPA and image checksums.
5. Archive all artifacts and bind the result to commit SHA, provider versions and test level.

## Current repository status

This profile does not enable production mutations. The current branch has read-only provider discovery and CRD-backed manifest validation, but it does not yet have a real CAPI/Metal3/Ironic provisioning adapter, target-agent bootstrap executor, or end-to-end pivot/reclaim implementation. Do not report upstream CI success as a test pass for this repository. The next implementation increment is a pinned provider compatibility profile and a non-mutating preflight that evaluates the gates above; mutation adapters remain disabled until the LIBVIRT_IRONIC test path passes.
