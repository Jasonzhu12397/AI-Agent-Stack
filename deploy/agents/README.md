# Kubernetes deployment templates for the two node-agent roles

These manifests are templates. They intentionally do not grant Kubernetes write permissions.

The EPH Agent runs inside a temporary single-node Kubernetes cluster on a RAM-booted bare-metal host. That host is borrowed from the final target compute pool and must later be rebooted into the target worker OS/image and verified by the Target Agent as a Ready worker. The EPH OS, local etcd, Pod filesystem and local credentials are disposable; persist deployment state centrally. See [the EPH lifecycle specification](../../docs/eph-node-lifecycle.md).

## Cloud and physical-site deployment topology

Deploy the Studio API, orchestration/worker services, database and model-routing services in the selected cloud environment (Alibaba Cloud or Tencent Cloud). The EPH and Target Agent Pods are deployed on physical infrastructure, not in the cloud Control Plane. Both agents initiate outbound HTTPS/mTLS sessions to the configured cloud endpoint. Permit only the required outbound DNS, time synchronization, registry and Control Plane destinations; do not expose Kubernetes API, Ironic, BMC or an inbound Agent port to the public Internet.

The target cluster has separate Control Plane and Data Plane networks. Keep node-originated management traffic (including kubelet-to-API and host-level CNI operations) on the ordinary Control Plane NIC. Pod-originated traffic, including EPH/Target Agent Pods, defaults to the Data Plane; configure the primary Pod network/default route accordingly. Configure Calico as the primary CNI and Multus for secondary attachments. OVS CNI and SR-IOV CNI are selected per NetworkAttachmentDefinition. DPDK additionally requires host VFIO/IOMMU, hugepages, NUMA/CPU pinning and device-plugin resources. Do not configure multiple competing primary CNIs or redirect node-management traffic onto an isolated Data Plane.

Validate actual Pod interfaces, IP addresses, MTU, routes and Data Plane reachability, including cloud Control Plane reachability from both Agent Pods over the intended Data Plane egress path. Verify that loss of the Data Plane is detected and reported; Kubernetes node management remains reachable through the Control Plane NIC. See [the dual-network architecture](../../docs/dual-agent-infrastructure-architecture.md#dual-network-kubernetes-and-pod-traffic-policy) before applying environment-specific CNI manifests.

## Build and publish the agent image

Build the repository image and push it to a registry accessible by both management and target clusters. Replace `REPLACE_WITH_REGISTRY/ai-agent-stack:VERSION` in both Deployment manifests with the exact immutable image tag or digest.

## Build the CAPI pivot preflight/dry-run node-agent image

Build the dedicated node-agent image with a Cluster API `clusterctl` version selected from the deployment compatibility matrix. The current Agent uses `kubectl` for API discovery, read-only preflight and postflight checks, and `clusterctl` for `clusterctl move --dry-run` only. The current runtime does not execute a mutating CAPI move or transfer object ownership. Both tool versions and checksums are mandatory and must come from trusted release artifacts used by your build pipeline:

~~~bash
docker login <registry>

IMAGE_REPOSITORY=<registry>/ai-agent-node-agent \
IMAGE_TAG=<immutable-build-tag> \
CLUSTERCTL_VERSION=<verified-release-tag> \
CLUSTERCTL_SHA256_AMD64=<verified-clusterctl-amd64-sha256> \
CLUSTERCTL_SHA256_ARM64=<verified-clusterctl-arm64-sha256> \
KUBECTL_VERSION=<verified-kubernetes-release-tag> \
KUBECTL_SHA256_AMD64=<verified-kubectl-amd64-sha256> \
KUBECTL_SHA256_ARM64=<verified-kubectl-arm64-sha256> \
PLATFORMS=linux/amd64 \
bash deploy/agents/build-node-agent.sh
~~~

The build script pushes the image and prints an immutable AGENT_IMAGE_REF=repository@sha256:... value. Pass that exact digest to the deployment helper below. For multi-architecture clusters, also provide CLUSTERCTL_SHA256_ARM64 and KUBECTL_SHA256_ARM64, then set PLATFORMS=linux/amd64,linux/arm64.

Do not guess the Cluster API version or checksum. The image build fails if either argument is missing or the checksum does not match. Use this image in both Agent Deployment templates for discovery, pivot preflight/dry-run and read-only postflight. A mutating `clusterctl move` executor is not enabled in the current runtime. The kubectl version must be compatible with the target cluster API server (prefer the same minor version); do not use an unverified latest download.

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
5. The Target Agent reads those ConfigMaps, verifies that each ConfigMap name matches the deterministic node-name-derived identity, validates the report schema and hardware collection types, correlates each report with the live Node UID, ignores stale reports from replaced nodes, and returns the per-node NIC/PCI/VF/IOMMU/NUMA/hugepage inventory in `cluster.network.inspect`. A malformed or misbound report is excluded and leaves that node in `missing_node_names`; it cannot satisfy the inventory-complete gate.

The DaemonSet does not use host networking, host PID, privileged mode, or writable host mounts. Its Pods reach the Kubernetes API over their normal Pod network, which must be routed on the Data Plane as required by the site policy. A read-only hostPath admission exception for `/sys` is required; if prohibited, do not weaken the Agent security context—use a separately approved host inventory integration. Reports contain hardware inventory (including MAC addresses), not BMC credentials or Pod secrets.

## Target Cluster Agent (target cluster)

The target agent must not be required to create the first target Control Plane. As soon as the first Control Plane API, etcd and core networking are Ready, install the Target Agent before provisioning Data Plane workers. The supplied Deployment selects a control-plane node and tolerates the standard control-plane NoSchedule taint, so it can schedule when no workers exist.

1. Enroll a second node with type `target_cluster` and issue a separate target-cluster credential.
2. Create `target-cluster-agent-credentials` in `ai-agent-system` with keys `control-url`, `node-id` and `token`.
3. Apply `namespace.yaml`, `rbac.yaml` and `target-cluster-agent.yaml` to the target cluster.
4. Verify the Pod is Ready and its Supervisor heartbeat/command lease works.
5. When the Supervisor assigns a deployment through the handoff API, the Target Agent discovers it in its heartbeat response and automatically retrieves its durable checkpoint before polling commands. Confirm the structured `deployment_checkpoint_restored` log event and the expected phase/sequence.
6. If CAPI controllers and Cluster API objects were hosted in EPH, the current Agent can run `clusterctl move --dry-run` and validate target prerequisites only. It does not perform the move. Do not stop EPH or claim Target ownership from a successful dry-run. A real pivot requires a separately enabled, version-tested execution procedure and independent source/target reconciliation checks.
7. The current Worker Provision command also runs server-side dry-run only: it returns a hashed MachineDeployment manifest preview, creates no MachineDeployment/Machine/Node, does not queue postflight and leaves the deployment at `plan_ready`. Remaining Control Plane replicas and Data Plane workers require a separately enabled execution adapter; they are not provisioned by this runtime.
8. EPH re-image/reboot and worker re-enrollment are not automated. Do not reclaim the original EPH hardware until a future reclaim executor has verified Target Agent takeover, CAPI ownership, worker health, network readiness and credential revocation.

The Worker Provision preflight validates that the `Metal3MachineTemplate` references a same-namespace `Metal3DataTemplate`, that both carry the expected `ai-agent-stack.io/network-profile-hash`, and that the template's declared control-plane/data-plane links, interface IDs, MAC mappings and MTUs match the Network Profile. It also checks the selected BareMetalHost inventory and available capacity. The approved Worker Provision command then submits a Kubernetes Server-Side Dry-Run for the proposed MachineDeployment and returns a manifest hash. In the current runtime it does not create the MachineDeployment, so CAPM3 does not create Machines, Metal3Machines, Metal3DataClaims, Metal3Data or rendered Secrets through this command. No worker readiness or rendered network-data verification is claimed. A future execution adapter must add those checks after actual provisioning and must be tested against the exact CAPI/CAPM3/Metal3/Ironic versions. Even rendered network data does not prove live SR-IOV VF binding, DPDK dataplane operation or Pod connectivity; those require hardware-backed and site-level traffic tests.

The supplied target Role/RoleBinding grants read access to pods, events and services in the `default` namespace only. Add equivalent Role/RoleBinding pairs for each explicitly approved workload namespace. The ClusterRole grants read-only node/namespace discovery.

The Target Agent's CAPI worker dry-run role has a separate least-privilege grant in `rbac.yaml`: read access to Cluster/Machine/template/Metal3Data resources, `get` on Secrets, and namespaced `get/list/create` on MachineDeployments. Kubernetes authorizes Server-Side Dry-Run using the same `create` verb, so the verb remains present even though the current Agent helper rejects every non-dry-run request with `MACHINE_DEPLOYMENT_REAL_WRITES_DISABLED`. Treat this as residual privilege: keep the RoleBindings limited to the dedicated `default` and `metal3` namespaces, use no unrelated application credentials there, and do not add patch/update/delete or cluster-admin permissions. The Secret grant is `get` only; because that can read any Secret by name in the namespace, use dedicated namespaces or a narrower external Secret-inspection service. A separate Metal3 inventory ClusterRole is bound only in `metal3-system` and `metal3`.

## Mandatory Agent mTLS credentials

Both EPH and Target Agent Deployments now require a client certificate. Before running `deploy.sh`, create a separate `agent-control-plane-mtls` Secret in **each** cluster (the certificate may be unique per Agent/node; do not reuse a private key across physical hosts). The Secret must contain `ca.crt` (the CA that verifies the cloud ingress server), `tls.crt` (the node client certificate) and `tls.key` (the matching private key):

```sh
kubectl --context "$EPH_CONTEXT" -n ai-agent-system create secret generic agent-control-plane-mtls \
  --from-file=ca.crt=/secure/path/control-plane-ca.crt \
  --from-file=tls.crt=/secure/path/eph-agent-client.crt \
  --from-file=tls.key=/secure/path/eph-agent-client.key

kubectl --context "$TARGET_CONTEXT" -n ai-agent-system create secret generic agent-control-plane-mtls \
  --from-file=ca.crt=/secure/path/control-plane-ca.crt \
  --from-file=tls.crt=/secure/path/target-agent-client.crt \
  --from-file=tls.key=/secure/path/target-agent-client.key
```

Before binding a certificate, calculate the exact fingerprint using the same Agent runtime (this prints only the public certificate fingerprint and does not require the Agent enrollment token):

```sh
AGENT_TLS_CERT_FILE=/secure/path/eph-agent-client.crt \
  python -m apps.node_agent.agent --print-client-cert-fingerprint
```

Run it separately for the Target Agent certificate. Bind each output to its enrolled node through the tenant-admin API:

```http
PUT /api/v1/agent-nodes/{node_id}/client-certificate/
Content-Type: application/json

{"fingerprint_sha256":"<64-character-lowercase-sha256>"}
```

Use a client certificate signed by the CA configured on the cloud HAProxy ingress. Bind the SHA-256 fingerprint of each certificate's DER encoding to its enrolled Agent node through the tenant-admin client-certificate API before starting the Agent. The cloud ingress verifies the certificate during the TLS handshake and forwards only its computed SHA-256 fingerprint to Django; the API also checks that the request came from the dedicated ingress IP and that the fingerprint matches the node binding. The Agent sets `AGENT_TLS_CLIENT_CERT_REQUIRED=true` and fails at startup if its certificate/key pair is absent or incomplete. TLS server verification remains enabled; do not use self-signed untrusted leaf certificates, disable hostname verification, or route around the dedicated mTLS ingress.

For certificate rotation, issue a new certificate, update the Secret, bind the new DER SHA-256 fingerprint to the node, and restart/roll out the Agent. The old certificate is rejected immediately after the binding changes. Revoke the node binding when retiring an Agent. Keep CA private keys and client private keys in an external secret manager; only the public CA certificate and node-specific client credentials belong in the Kubernetes Secret. Restrict Secret read access to cluster operators and the Agent Pod's volume mount.

## Security and operation

- The Deployment runs as UID/GID 10001, drops all Linux capabilities, disallows privilege escalation, uses a read-only root filesystem and the RuntimeDefault seccomp profile.
- The only writable mount is a size-limited `emptyDir` for process/readiness probe timestamps.
- Both Agent Deployments mount host `/sys` read-only at `/host/sys` for physical NIC, PCI PF/VF, SR-IOV VF count, NUMA and hugepage inventory. They do not use `hostNetwork`, `hostPID`, privileged mode or writable host mounts. The Kubernetes Pod Security Admission policy must explicitly permit this read-only `hostPath`; if site policy forbids hostPath, use a separately reviewed node hardware-inventory DaemonSet/extension rather than granting broad privilege.
- Readiness requires a successful control-plane heartbeat/poll. Liveness checks that the process loop is still running.
- Agents initiate outbound HTTPS; no inbound Service or SSH listener is exposed.
- Create credentials with an external Secret Manager / External Secrets integration in production. Do not commit token values or plaintext Secret manifests.
- The EPH RBAC remains read-only. Target Agent MachineDeployment `create` authorization is retained only for Kubernetes Server-Side Dry-Run; the Agent helper rejects non-dry-run writes. Metal3 BMH access remains read-only. Do not add `cluster-admin` as a workaround.
- The initial bearer token is a development enrollment mechanism. Production must replace it with mTLS or short-lived workload identity before broad deployment.


## Direct Ironic API verification for Pivot postflight

The Target Agent's `cluster.capi.pivot.postflight` command checks the CAPI Machine -> Metal3Machine -> BareMetalHost chain. To include direct Ironic evidence, provide an HTTPS Ironic API endpoint and a scoped Ironic API token through the Kubernetes Secret `ironic-api-credentials` in `ai-agent-system`. The Target Agent Deployment reads keys `api-url` and `token` as `IRONIC_API_URL` and `IRONIC_API_TOKEN`. The endpoint must be the Ironic API root (for example, `https://ironic-api.example.net`), without a path, query or fragment. The client validates the server certificate using the container trust store, uses the Ironic token header, disables redirects, and only performs a bounded read-only GET of the node UUID recorded by BMO.

Create the Secret through an external secret manager/External Secrets Operator in production. For an isolated lab only, a manually created Secret can be populated from protected shell variables; do not put real token values in shell history, CI logs, Git, or Agent command payloads. If either Secret key is absent, the Agent reports Ironic as `not_configured`. The Supervisor intentionally refuses to advance from Pivot postflight to Worker provisioning unless every applicable provisioned host has a healthy direct Ironic node record. A Kubernetes-only BMH check is not considered complete hardware verification.

The current automated sequence is deliberately simulation-only:

1. EPH runs `clusterctl move --dry-run` and validates source/target prerequisites; it does not execute the move or transfer ownership.
2. The Target Agent's postflight command is read-only and can inspect CAPI/Metal3/BMH/Ironic state only after a separately performed pivot; it is not proof that the automated runtime performed that pivot.
3. Worker Provision runs Kubernetes Server-Side Dry-Run only, stores a manifest hash, leaves the deployment at `plan_ready` and does not queue postflight.
4. No worker Machines or Nodes are created by the current automated flow.
5. EPH reclaim remains disabled; it requires a separate lifecycle/reclaim executor and physical Data Plane validation.

## Deploy both Agent roles to separate clusters

The deployment helper applies the EPH resources to the temporary EPH cluster and the Target Agent plus per-node network inventory DaemonSet to the target cluster. It requires two different, already configured kubeconfig contexts and a digest-pinned image built from Dockerfile.node-agent.

First, build and push the node-agent image using verified Cluster API and Kubernetes release checksums as described above. Use the resulting registry digest, not a mutable tag. Then create the namespaces and the required Secrets. Supply secret values through files managed by your secret manager; do not put tokens or kubeconfig contents in Git or shell command arguments.

~~~bash
kubectl --context "$EPH_CONTEXT" apply -f deploy/agents/namespace.yaml
kubectl --context "$TARGET_CONTEXT" apply -f deploy/agents/namespace.yaml

kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system create secret generic eph-node-agent-credentials \
  --from-file=control-url=/secure/path/eph-control-url \
  --from-file=node-id=/secure/path/eph-node-id \
  --from-file=token=/secure/path/eph-agent-token

kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system create secret generic capi-pivot-kubeconfigs \
  --from-file=source.kubeconfig=/secure/path/source.kubeconfig \
  --from-file=target.kubeconfig=/secure/path/target.kubeconfig

kubectl --context "$TARGET_CONTEXT" --namespace ai-agent-system create secret generic target-cluster-agent-credentials \
  --from-file=control-url=/secure/path/target-control-url \
  --from-file=node-id=/secure/path/target-node-id \
  --from-file=token=/secure/path/target-agent-token

kubectl --context "$TARGET_CONTEXT" --namespace ai-agent-system create secret generic ironic-api-credentials \
  --from-file=api-url=/secure/path/ironic-api-url \
  --from-file=token=/secure/path/ironic-readonly-token
~~~

For secret rotation, use the kubectl create secret generic --dry-run=client -o yaml | kubectl apply -f - pattern with the same --from-file inputs. Verify the Ironic token is read-only and that the API endpoint is reachable from the Target Agent Pod over the intended network.

Run the deployment helper after setting the two context names and immutable image digest:

~~~bash
EPH_CONTEXT=<eph-context> \
TARGET_CONTEXT=<target-context> \
AGENT_IMAGE=<registry>/ai-agent-node-agent@sha256:<64-hex-digest> \
bash deploy/agents/deploy.sh
~~~

The script verifies both API servers, checks required Secret keys without printing their values, server-side dry-runs all Agent workloads, applies them, and waits for both Deployments and the inventory DaemonSet to roll out. A successful rollout proves only that the Agent Pods started and became Ready. It does not prove CAPI pivot, Ironic provisioning, SR-IOV/DPDK traffic, or EPH reclaim; those require their respective runtime postflight and hardware tests. The script intentionally does not create the Cloud Control Plane services or apply site-specific Calico, Multus, OVS, SR-IOV CNI, device-plugin or DPDK configuration.
