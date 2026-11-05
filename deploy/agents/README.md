# Kubernetes deployment templates for the two node-agent roles

These manifests are templates. They intentionally do not grant Kubernetes write permissions.

The EPH Agent runs inside a temporary single-node Kubernetes cluster on a RAM-booted bare-metal host. That host is borrowed from the final target compute pool and must later be rebooted into the target worker OS/image and verified by the Target Agent as a Ready worker. The EPH OS, local etcd, Pod filesystem and local credentials are disposable; persist deployment state centrally. See [the EPH lifecycle specification](../../docs/eph-node-lifecycle.md).

## Cloud and physical-site deployment topology

Deploy the Studio API, orchestration/worker services, database and model-routing services in the selected cloud environment (Alibaba Cloud or Tencent Cloud). The EPH and Target Agent Pods are deployed on physical infrastructure, not in the cloud Control Plane. Both agents initiate outbound HTTPS/mTLS sessions to the configured cloud endpoint. Permit only the required outbound DNS, time synchronization, registry and Control Plane destinations; do not expose Kubernetes API, Ironic, BMC or an inbound Agent port to the public Internet.

The target cluster has separate Control Plane and Data Plane networks. Keep node-originated management traffic (including kubelet-to-API and host-level CNI operations) on the ordinary Control Plane NIC. Pod-originated traffic, including EPH/Target Agent Pods, defaults to the Data Plane; configure the primary Pod network/default route accordingly. Configure Calico as the primary CNI and Multus for secondary attachments. OVS CNI and SR-IOV CNI are selected per NetworkAttachmentDefinition. DPDK additionally requires host VFIO/IOMMU, hugepages, NUMA/CPU pinning and device-plugin resources. Do not configure multiple competing primary CNIs or redirect node-management traffic onto an isolated Data Plane.

Validate actual Pod interfaces, IP addresses, MTU, routes and Data Plane reachability, including cloud Control Plane reachability from both Agent Pods over the intended Data Plane egress path. Verify that loss of the Data Plane is detected and reported; Kubernetes node management remains reachable through the Control Plane NIC. See [the dual-network architecture](../../docs/dual-agent-infrastructure-architecture.md#dual-network-kubernetes-and-pod-traffic-policy) before applying environment-specific CNI manifests.

## Build and publish the agent image

Build the repository image and push it to a registry accessible by both management and target clusters. Replace `REPLACE_WITH_REGISTRY/ai-agent-stack:VERSION` in both Deployment manifests with the exact immutable image tag or digest.

## Build the CAPI pivot-capable node-agent image

Build the dedicated node-agent image with a Cluster API `clusterctl` version selected from the deployment compatibility matrix. The checksum is mandatory and must be obtained from the trusted release artifact used by your build pipeline:

```bash
docker build -f Dockerfile.node-agent \
  --build-arg CLUSTERCTL_VERSION=<verified-release-tag> \
  --build-arg CLUSTERCTL_SHA256=<verified-linux-amd64-or-arm64-sha256> \
  -t <registry>/ai-agent-node-agent:<immutable-tag> .
```

Do not guess the Cluster API version or checksum. The image build fails if either argument is missing or the checksum does not match. Use this image in both node-agent Deployment templates when enabling the pivot dry-run command.

The EPH Agent's optional `cluster.capi.pivot.dry-run` command requires a Secret named `capi-pivot-kubeconfigs` in `ai-agent-system` with keys `source.kubeconfig` and `target.kubeconfig`. These are mounted read-only under `/var/run/secrets/ai-agent/capi-pivot`; the source file identifies the temporary EPH management cluster and the target file identifies the target management cluster. Grant only the Kubernetes permissions required for discovery and CAPI move dry-run. Do not place kubeconfig contents in command payloads or Git.

The dry-run invokes `clusterctl move --dry-run` only. It reports that the namespace scope includes all CAPI objects in the selected namespace; it does not perform a move, install providers, bootstrap the Target Agent, or authorize worker provisioning. A successful dry-run is not proof that a real pivot is safe.

## EPH Node Agent (temporary RAM-boot EPH cluster)

1. In the Studio, enroll a node with type `eph_node` in the intended tenant and project.
2. Store the returned node UUID and one-time token in your secret manager.
3. Create the Kubernetes Secret `eph-node-agent-credentials` in namespace `ai-agent-system` with keys `control-url`, `node-id` and `token`.
4. Apply `namespace.yaml`, `rbac.yaml` and `eph-node-agent.yaml`.

The EPH ServiceAccount is read-only for core node/namespace data, CAPI resources and Metal3 resources. It can discover API groups and run preflight/plan checks. It cannot create or modify Cluster, Machine, BareMetalHost or BMC resources. Do not treat this single-node Kubernetes cluster as a persistent management plane. Complete the bootstrap work, persist all required state centrally, revoke the EPH runtime identity, then hand off/reboot the physical host into the final target worker OS.

## Per-node physical network inventory (target cluster)

The Target Agent Deployment is pinned to a control-plane node so it can start before workers exist. Its own host sysfs mount therefore describes only that one physical server. To inventory every physical target node (especially Data Plane workers with SR-IOV VFs/DPDK), also apply `network-inventory-agent.yaml` as a DaemonSet:

1. Apply `namespace.yaml` and `rbac.yaml`.
2. Replace the DaemonSet image with the same immutable node-agent image used by the Target Agent.
3. Apply `network-inventory-agent.yaml` to schedule one small, non-root publisher on every node, including tainted control-plane nodes.
4. Each publisher reads host `/sys` read-only and writes a bounded hardware report to a node-specific ConfigMap in `ai-agent-inventory`. It has only `get` access to Nodes and `get/create/update` access to ConfigMaps in `ai-agent-inventory`. The publisher's Kubernetes API client validates the service host and HTTPS port, restricts requests to the exact Node-read and inventory-ConfigMap endpoints, validates the ServiceAccount token, and disables redirects so the bearer token cannot be forwarded to another origin. A redirect is treated as an error; configure the in-cluster API endpoint directly rather than relying on HTTP redirects.
5. The Target Agent reads those ConfigMaps, correlates each report with the live Node UID, ignores stale reports from replaced nodes, and returns the per-node NIC/PCI/VF/IOMMU/NUMA/hugepage inventory in `cluster.network.inspect`.

The DaemonSet does not use host networking, host PID, privileged mode, or writable host mounts. Its Pods reach the Kubernetes API over their normal Pod network, which must be routed on the Data Plane as required by the site policy. A read-only hostPath admission exception for `/sys` is required; if prohibited, do not weaken the Agent security context—use a separately approved host inventory integration. Reports contain hardware inventory (including MAC addresses), not BMC credentials or Pod secrets.

## Target Cluster Agent (target cluster)

The target agent must not be required to create the first target Control Plane. As soon as the first Control Plane API, etcd and core networking are Ready, install the Target Agent before provisioning Data Plane workers. The supplied Deployment selects a control-plane node and tolerates the standard control-plane NoSchedule taint, so it can schedule when no workers exist.

1. Enroll a second node with type `target_cluster` and issue a separate target-cluster credential.
2. Create `target-cluster-agent-credentials` in `ai-agent-system` with keys `control-url`, `node-id` and `token`.
3. Apply `namespace.yaml`, `rbac.yaml` and `target-cluster-agent.yaml` to the target cluster.
4. Verify the Pod is Ready and its Supervisor heartbeat/command lease works.
5. When the Supervisor assigns a deployment through the handoff API, the Target Agent discovers it in its heartbeat response and automatically retrieves its durable checkpoint before polling commands. Confirm the structured `deployment_checkpoint_restored` log event and the expected phase/sequence.
6. If CAPI controllers and Cluster API objects were hosted in EPH, pivot them into the target cluster with `clusterctl move` (or a provider/version-tested equivalent) and verify reconciliation before stopping EPH or provisioning workers.
7. Continue remaining Control Plane replicas and Data Plane workers from the target cluster management plane.
8. Re-image/reboot the original EPH hardware only after it is no longer needed as the bootstrap host; enroll it as a worker with a new identity and verify its physical identity and Ready condition.

The supplied target Role/RoleBinding grants read access to pods, events and services in the `default` namespace only. Add equivalent Role/RoleBinding pairs for each explicitly approved workload namespace. The ClusterRole grants read-only node/namespace discovery.

The target Agent's CAPI worker executor has a separate least-privilege grant in `rbac.yaml`: a ClusterRole containing read access to Cluster/Machine/template resources and `get/list/create` on MachineDeployments, bound only within the `default` and `metal3` namespaces. A separate Metal3 inventory ClusterRole is bound only in `metal3-system` and `metal3`. The Agent does not receive patch, update, delete, BMH mutation, Secret, or cluster-admin permissions. If the target CAPI namespace or BMH namespace differs, add a RoleBinding to the corresponding ClusterRole in that exact namespace; do not bind these roles cluster-wide. The CAPI API Server-Side Dry-Run remains the final RBAC/schema gate before creation.

## Security and operation

- The Deployment runs as UID/GID 10001, drops all Linux capabilities, disallows privilege escalation, uses a read-only root filesystem and the RuntimeDefault seccomp profile.
- The only writable mount is a size-limited `emptyDir` for process/readiness probe timestamps.
- Both Agent Deployments mount host `/sys` read-only at `/host/sys` for physical NIC, PCI PF/VF, SR-IOV VF count, NUMA and hugepage inventory. They do not use `hostNetwork`, `hostPID`, privileged mode or writable host mounts. The Kubernetes Pod Security Admission policy must explicitly permit this read-only `hostPath`; if site policy forbids hostPath, use a separately reviewed node hardware-inventory DaemonSet/extension rather than granting broad privilege.
- Readiness requires a successful control-plane heartbeat/poll. Liveness checks that the process loop is still running.
- Agents initiate outbound HTTPS; no inbound Service or SSH listener is exposed.
- Create credentials with an external Secret Manager / External Secrets integration in production. Do not commit token values or plaintext Secret manifests.
- The EPH RBAC remains read-only. The Target Agent has only the namespaced CAPI MachineDeployment create permission described above; Metal3 BMH access is read-only. Do not add `cluster-admin` as a workaround.
- The initial bearer token is a development enrollment mechanism. Production must replace it with mTLS or short-lived workload identity before broad deployment.
