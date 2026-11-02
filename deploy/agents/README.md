# Kubernetes deployment templates for the two node-agent roles

These manifests are templates. They intentionally do not grant Kubernetes write permissions.

The EPH Agent runs inside a temporary single-node Kubernetes cluster on a RAM-booted bare-metal host. That host is borrowed from the final target compute pool and must later be rebooted into the target worker OS/image and verified by the Target Agent as a Ready worker. The EPH OS, local etcd, Pod filesystem and local credentials are disposable; persist deployment state centrally. See [the EPH lifecycle specification](../../docs/eph-node-lifecycle.md).

## Build and publish the agent image

Build the repository image and push it to a registry accessible by both management and target clusters. Replace `REPLACE_WITH_REGISTRY/ai-agent-stack:VERSION` in both Deployment manifests with the exact immutable image tag or digest.

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
