# Kubernetes deployment templates for the two node-agent roles

These manifests are templates. They intentionally do not grant Kubernetes write permissions.

The EPH Agent runs inside a temporary single-node Kubernetes cluster on a RAM-booted bare-metal host. That host is borrowed from the final target compute pool and must later be rebooted into the target worker OS/image and verified by the Target Agent as a Ready worker. The EPH OS, local etcd, Pod filesystem and local credentials are disposable; persist deployment state centrally. See [the EPH lifecycle specification](../../docs/eph-node-lifecycle.md).

## Cloud and physical-site deployment topology

Deploy the Studio API, orchestration/worker services, database and model-routing services in the selected cloud environment (Alibaba Cloud or Tencent Cloud). The EPH and Target Agent Pods are deployed on physical infrastructure, not in the cloud Control Plane. Both agents initiate outbound HTTPS/mTLS sessions to the configured cloud endpoint. Permit only the required outbound DNS, time synchronization, registry and Control Plane destinations; do not expose Kubernetes API, Ironic, BMC or an inbound Agent port to the public Internet.

The target cluster has separate Control Plane and Data Plane networks. Keep node management, Kubernetes API and Agent-to-cloud connectivity on the ordinary Control Plane NIC. Configure Calico as the primary Pod network and Multus for secondary attachments. OVS CNI and SR-IOV CNI are selected per NetworkAttachmentDefinition. DPDK additionally requires host VFIO/IOMMU, hugepages, NUMA/CPU pinning and device-plugin resources. Do not configure multiple competing primary CNIs or redirect node-management traffic onto an isolated Data Plane.

Workloads that require high-performance networking must declare their Data Plane attachment and route policy. Validate the actual Pod interfaces, IP addresses, MTU, routes and Data Plane reachability. Verify that loss of the Data Plane does not break Kubernetes API access or either Agent's cloud heartbeat. See [the dual-network architecture](../../docs/dual-agent-infrastructure-architecture.md#dual-network-kubernetes-and-pod-traffic-policy) before applying environment-specific CNI manifests.

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

## Security and operation

- The Deployment runs as UID/GID 10001, drops all Linux capabilities, disallows privilege escalation, uses a read-only root filesystem and the RuntimeDefault seccomp profile.
- The only writable mount is a size-limited `emptyDir` for process/readiness probe timestamps.
- Readiness requires a successful control-plane heartbeat/poll. Liveness checks that the process loop is still running.
- Agents initiate outbound HTTPS; no inbound Service or SSH listener is exposed.
- Create credentials with an external Secret Manager / External Secrets integration in production. Do not commit token values or plaintext Secret manifests.
- The current EPH and target RBAC are read-only. The actual CAPI/Metal3/Ironic provisioning adapter is not enabled by these manifests. Do not add `cluster-admin` as a workaround.
- The initial bearer token is a development enrollment mechanism. Production must replace it with mTLS or short-lived workload identity before broad deployment.
