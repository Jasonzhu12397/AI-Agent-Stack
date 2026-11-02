# Kubernetes deployment templates for the two node-agent roles

These manifests are templates. They intentionally do not grant Kubernetes write permissions.

## Build and publish the agent image

Build the repository image and push it to a registry accessible by both management and target clusters. Replace `REPLACE_WITH_REGISTRY/ai-agent-stack:VERSION` in both Deployment manifests with the exact immutable image tag or digest.

## EPH Node Agent (management / EPH cluster)

1. In the Studio, enroll a node with type `eph_node` in the intended tenant and project.
2. Store the returned node UUID and one-time token in your secret manager.
3. Create the Kubernetes Secret `eph-node-agent-credentials` in namespace `ai-agent-system` with keys `control-url`, `node-id` and `token`.
4. Apply `namespace.yaml`, `rbac.yaml` and `eph-node-agent.yaml`.

The EPH ServiceAccount is read-only for core node/namespace data, CAPI resources and Metal3 resources. It can discover API groups and run preflight/plan checks. It cannot create or modify Cluster, Machine, BareMetalHost or BMC resources.

## Target Cluster Agent (target cluster)

The target agent must not be required to create the target cluster. After the target API endpoint is available:

1. Enroll a second node with type `target_cluster`.
2. Create `target-cluster-agent-credentials` in `ai-agent-system` with the same three keys.
3. Apply `namespace.yaml`, `rbac.yaml` and `target-cluster-agent.yaml` to the target cluster.

The supplied target Role/RoleBinding grants read access to pods, events and services in the `default` namespace only. Add equivalent Role/RoleBinding pairs for each explicitly approved workload namespace. The ClusterRole grants read-only node/namespace discovery.

## Security and operation

- The Deployment runs as UID/GID 10001, drops all Linux capabilities, disallows privilege escalation, uses a read-only root filesystem and the RuntimeDefault seccomp profile.
- The only writable mount is a size-limited `emptyDir` for process/readiness probe timestamps.
- Readiness requires a successful control-plane heartbeat/poll. Liveness checks that the process loop is still running.
- Agents initiate outbound HTTPS; no inbound Service or SSH listener is exposed.
- Create credentials with an external Secret Manager / External Secrets integration in production. Do not commit token values or plaintext Secret manifests.
- The current EPH and target RBAC are read-only. The actual CAPI/Metal3/Ironic provisioning adapter is not enabled by these manifests. Do not add `cluster-admin` as a workaround.
- The initial bearer token is a development enrollment mechanism. Production must replace it with mTLS or short-lived workload identity before broad deployment.
