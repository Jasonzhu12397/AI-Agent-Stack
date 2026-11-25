# Three-Agent deployment

This directory deploys the new three-Agent architecture. The legacy Node Agent deployment path is not used by these manifests or scripts.

## Runtime topology

- **Cloud Control Agent**: cloud-side durable command coordinator and mTLS API. Deploy in the Alibaba Cloud or Tencent Cloud management environment.
- **EPH Agent**: one replica inside the temporary EPH Single-Node Kubernetes cluster. It performs source-side preflight, renders and applies the target control plane, and bootstraps the Target Agent into the Target Control Plane from the sealed `target-agent-bootstrap` bundle before handing over.
- **Software worker sidecar**: both `eph-agent` and `target-agent` Pods run `dev-agent-worker` by default. It claims only `kind: "software"` work and edits the repository mounted from the `software-workspace` PVC at `/workspace/repo`. `AGENT_SOFTWARE_WORKSPACE` points at the same path. No control-plane approval gates these pods; approval applies only to interactive software work outside the EPH/Target autonomy boundary.
- **Target Agent**: three replicas, spread one per Target Control Plane master. Kubernetes Lease leader election ensures only the elected replica polls and executes commands. All replicas expose readiness and report their own pod identity.

The Target Agent currently has live Kubernetes/CAPI/Metal3/Ironic observation, trusted Pivot ownership verification, a guarded EPH cleanup executor, a Control Plane chrony profile renderer restricted to approved external sources and Worker networks, a Worker NTP policy validator, and pre-kubeadm chrony command renderers for both roles. Worker MachineDeployment provisioning and network/storage/SR-IOV runtime resources are executable. Before the first write, the Target Agent checks every required RBAC rule with `kubectl auth can-i`; a missing rule returns `TARGET_RBAC_INSUFFICIENT:<verb>:<resource>` before any side effect. The `target-agent-metal3-operator` Role in `target-cluster-agent.yaml` carries the namespaced write rules and `target-agent-storageclass-writer` carries the StorageClass rule. The agent never receives wildcard permissions. Rook-Ceph and SR-IOV operator bundles remain a platform preinstall because their upstream manifests create cluster-scoped RBAC and CRDs outside the agent's authority. The generated Control Plane command installs chrony, configures only approved external sources, allows the Worker networks and verifies external synchronization plus UDP/123 listening. Target Agent inspects the CAPI Machine/KubeadmConfig bootstrap records for all Ready Control Plane Machines and requires the chrony verification command to be present; it also sends an actual NTP request to the Ironic internal IP/VIP over UDP/123 and rejects an unsynchronized response. The generated Worker command replaces the Worker source list with exactly the Ironic internal IP/VIP and verifies that chronyd selected that endpoint. The commands are not yet injected automatically into CAPI bootstrap templates or executed on newly provisioned physical hosts, so Worker host synchronization remains unverified.

## Required contexts and image references

The deployment script requires three distinct kubeconfig contexts:

- `CLOUD_CONTEXT`
- `EPH_CONTEXT`
- `TARGET_CONTEXT`

All images must be pinned by immutable SHA-256 digest and use the same EPH
host-local registry endpoint. The deploy script rejects any Cloud Control or
Target Agent image hosted in a different registry:

- `CLOUD_CONTROL_IMAGE`
- `EPH_AGENT_IMAGE`
- `TARGET_AGENT_IMAGE`
- `DEPLOYMENT_ID`: stable DNS-label identifier shared by the EPH and Target cleanup scope.
- `SOFTWARE_WORKSPACE_SIZE`: size of the `eph-agent-software-workspace` and `target-agent-software-workspace` PVCs, default `1Gi`; the cluster must provide a default StorageClass (for example Rook-Ceph or another open-source provisioner).

Run `deploy_agents.py` only after the secrets below have been provisioned by your secret manager. The entry point performs server-side dry-run validation before applying resources and waits for all three Deployments.

The Cloud Control manifest also deploys `cloud-control-reconcile`, a CronJob that
runs `python -m apps.cloud_control_agent.main --reconcile-once` every minute. It
uses the same database secret and image, replays interrupted auto-advance
decisions, and skips deployments that still own a queued or leased command.

## Required Secrets

### Cloud management cluster

- `cloud-control-agent-database`: key `database-url` (PostgreSQL connection string).
- `cloud-control-agent-mtls`: keys `tls.crt`, `tls.key`, `client-ca.crt`.

The Cloud Control API listens on TCP/8443 and is exposed as a LoadBalancer Service. Configure the cloud provider's load-balancer policy and DNS so EPH and Target can reach it over HTTPS. The server requires client certificates. Use a TCP/L4 load balancer with TLS passthrough; a TLS-terminating load balancer would break end-to-end mTLS unless it is explicitly configured to re-authenticate and forward the verified client identity through a separate trusted mechanism.

### EPH cluster

- `eph-agent-credentials`: `control-url`, `agent-id`, `installation-id`.
- `agent-control-plane-mtls`: `ca.crt`, `tls.crt`, `tls.key`.
- `capi-pivot-source-kubeconfig`: key `kubeconfig` (required at EPH bootstrap).
- `capi-pivot-target-kubeconfig`: key `kubeconfig` (optional until the Target Control Plane API is ready; the Target Agent bootstrap action requires it).
- `target-agent-bootstrap`: key `bundle.yaml`; the sealed Target Agent workload, credentials and mTLS document set. The EPH Agent applies it to the Target Control Plane and verifies the rollout before `target_agent_ready`. The bundle is never read from a command payload.

The EPH client certificate Common Name must equal the configured Agent UUID. EPH identity must not include a Target cluster ID.

### Target cluster

- `target-agent-credentials`: `control-url`, `agent-id`, `installation-id`, `cluster-id`, `ironic-api-url`, `ironic-api-token`, `ironic-internal-ntp-ip`. The Ironic API endpoint must use HTTPS and its certificate must chain to a CA trusted by the Target Agent image. The NTP IP is a separately pinned internal service/VIP address; Target Agent rejects command payloads that request any other NTP server. The API token is loaded from the Kubernetes Secret and is never included in command payloads or logs.
- `agent-control-plane-mtls`: `ca.crt`, `tls.crt`, `tls.key`.
- `target-agent-source-observer`: key `kubeconfig`; the Target Agent uses this source-cluster observer credential only through Pivot and cleanup, then revokes this Secret after successful EPH cleanup.

All three Target replicas share one logical Agent UUID and installation/cluster identity. Their per-pod `AGENT_INSTANCE_ID` is populated from the Pod name. The Target client certificate Common Name must equal the logical Agent UUID.

## Cloud Control database provisioning

The Cloud Control Agent creates its tables on startup. Before Agents can connect, register their identities in `agent_control_agents` and create a deployment record in `agent_control_deployments` with `phase='not_started'`, `sequence=0` and an empty JSON state. The Agent UUID, role, installation UUID and (for Target only) cluster UUID must match the mTLS certificate and Agent environment. Target Agent checkpoints are persisted through `POST /api/v1/agent-control/deployments/checkpoints`; updates are compare-and-swap protected by the current phase/sequence and the active command lease.

The Cloud Control API accepts:
- `POST /api/v1/agent-control/heartbeats` from registered EPH/Target Agents.
- `POST /api/v1/agent-control/commands/claim` from registered EPH/Target Agents.
- `POST /api/v1/agent-control/commands/lease/renew` from the current lease holder.
- `POST /api/v1/agent-control/commands/results` from the current lease holder.
- `POST /api/v1/control/commands` from a trusted platform API client certificate whose Common Name is `platform-api`.

The platform API command request must include `deployment_id`, `target_agent_id`, `required_role`, `action`, a unique `idempotency_key`, object `payload`, and a 64-character `approval_digest`. The API service certificate is the trust boundary for approval verification; the Cloud Control service records the digest but does not independently authenticate the human approver. Do not expose the platform command endpoint to untrusted clients.

Expired command leases can be reclaimed. The lease token is returned only to the current claimant and is stored as a SHA-256 digest; it is not persisted in the result record. The command envelope also carries the approval digest recorded by the trusted platform API. Lease renewal is bounded by the original absolute command deadline.

## Build

Use `build_agents.py` with immutable release versions and verified upstream checksums. Current EPH and Target image builds are verified only for `linux/amd64`; the script rejects other platforms rather than silently producing an unverified image. It writes local `.tar` archives and `.metadata.json` digest files under `deploy/agents/dist` (or `IMAGE_OUTPUT_DIR`) and never pushes to an external registry.

Example environment variables:

```bash
export IMAGE_TAG=2026.11.17-rc1
export EPH_LOCAL_REGISTRY=10.0.0.10:5000
export CLOUD_CONTROL_IMAGE_REPOSITORY=$EPH_LOCAL_REGISTRY/ai-agent-cloud-control-agent
export EPH_AGENT_IMAGE_REPOSITORY=$EPH_LOCAL_REGISTRY/ai-agent-eph-agent
export TARGET_AGENT_IMAGE_REPOSITORY=$EPH_LOCAL_REGISTRY/ai-agent-target-agent
export CLUSTERCTL_VERSION=v1.10.5
export CLUSTERCTL_SHA256_AMD64=<verified-64-character-sha256>
export KUBECTL_VERSION=v1.32.2
export KUBECTL_SHA256_AMD64=<verified-64-character-sha256>
python3 build_agents.py
```

The example versions/checksums are placeholders for your selected compatibility matrix; do not copy unverified values.

## Deploy

```bash
export CLOUD_CONTEXT=<cloud-management-context>
export EPH_CONTEXT=<temporary-eph-context>
export TARGET_CONTEXT=<target-cluster-context>
export CLOUD_CONTROL_IMAGE=<registry>/ai-agent-cloud-control-agent@sha256:<digest>
export EPH_AGENT_IMAGE=<registry>/ai-agent-eph-agent@sha256:<digest>
export TARGET_AGENT_IMAGE=<registry>/ai-agent-target-agent@sha256:<digest>
export DEPLOYMENT_ID=<stable-dns-label>
python3 deploy_agents.py
```

## Current implementation boundary

The current runtimes provide mTLS identity, heartbeat, command claim/result transport, lease renewal, PostgreSQL-backed command and Target checkpoint persistence, compare-and-swap checkpoint updates, Target leader election, ordered handoff evidence validation, live CAPI/Metal3 ownership observation, TLS-authenticated Ironic node verification for every target BMH, and Worker NTP policy validation/command rendering. The EPH cleanup executor now requires the Cloud Control trusted Pivot attestation and live Target Metal3/Ironic ownership verification; it deletes only resources explicitly labelled for the matching deployment scope, with source Cluster identity taken from the trusted Pivot attestation, verifies their absence, and removes the Target-side `target-agent-source-observer` Secret. `deploy_agents.py` applies the matching cleanup labels and requires `DEPLOYMENT_ID` and the observer Secret. The Worker Agent now has a bounded CAPI/CAPM3 provisioning executor: it discovers served CRD versions and schemas, checks existing resource ownership, validates the MachineDeployment/template references, verifies free ready BMH capacity and referenced Metal3 IPPools, injects the worker chrony bootstrap using only the Ironic internal IP, validates the control/data NIC contract, performs server-side dry-run, and applies templates before the MachineDeployment. Scale-down is deliberately blocked until a dedicated drain executor exists. These executors have unit-level fake-API coverage but still require live provider validation and failure/retry verification before production use. Final live network postflight (Calico/Multus/OVS/SR-IOV/DPDK and Pod data-plane routing), autonomous diagnosis/repair, and the full three-Agent lifecycle E2E remain incomplete.


After CAPI pivot, the EPH Agent binds cleanup eligibility to the attested source Cluster UID. It uses UID-preconditioned JSON patches to annotate the EPH Deployment, live pods, ServiceAccount, ClusterRole, ClusterRoleBinding, and deployment-scoped Secrets, and it annotates the Target-side source-observer Secret. Both the mounted Target kubeconfig and the Target-side source-observer kubeconfig are byte-compared with their corresponding Kubernetes Secret data before the scope is attested. Cloud Control persists trusted pivot evidence only when this cleanup scope and the observer Secret identity are verified; Target Agent repeats the ownership checks before deleting anything.


The Target Agent final postflight now revalidates the durable worker checkpoint digest, CAPI Cluster and KubeadmControlPlane readiness, MachineDeployment and worker Machine readiness, Ready worker Node count, Metal3/Ironic ownership, worker NTP bootstrap source attestations, Calico and Multus DaemonSets, SR-IOV CNI readiness, SR-IOV/OVS NetworkAttachmentDefinitions, and per-worker SR-IOV/DPDK allocatable resources. This verifies the deployed network stack and its advertised resources; it does not yet prove every workload Pod's live packet path end-to-end or collect continuous host chronyd tracking telemetry. A dedicated data-plane canary and node-level chrony telemetry remain required for that stronger guarantee.
