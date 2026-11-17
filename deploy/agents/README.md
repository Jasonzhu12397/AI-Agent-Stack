# Three-Agent deployment

This directory deploys the new three-Agent architecture. The legacy Node Agent deployment path is not used by these manifests or scripts.

## Runtime topology

- **Cloud Control Agent**: cloud-side durable command coordinator and mTLS API. Deploy in the Alibaba Cloud or Tencent Cloud management environment.
- **EPH Agent**: one replica inside the temporary EPH Single-Node Kubernetes cluster. It performs source-side preflight and reports readiness. Mutating CAPI Pivot remains blocked until its dedicated, approved adapter is implemented and tested.
- **Target Agent**: three replicas, spread one per Target Control Plane master. Kubernetes Lease leader election ensures only the elected replica polls and executes commands. All replicas expose readiness and report their own pod identity.

The Target Agent currently has read-only Kubernetes/CAPI/Metal3 observation handlers, a Control Plane chrony profile renderer restricted to approved external sources and Worker networks, a Worker NTP policy validator, and pre-kubeadm chrony command renderers for both roles. The EPH CAPI Pivot payload and approval requirements are specified in [CAPI Pivot contract](../../docs/capi-pivot-contract.md). The generated Control Plane command installs chrony, configures only approved external sources, allows the Worker networks and verifies external synchronization plus UDP/123 listening. Target Agent inspects the CAPI Machine/KubeadmConfig bootstrap records for all Ready Control Plane Machines and requires the chrony verification command to be present; it also sends an actual NTP request to the Ironic internal IP/VIP over UDP/123 and rejects an unsynchronized response. The generated Worker command replaces the Worker source list with exactly the Ironic internal IP/VIP and verifies that chronyd selected that endpoint. The commands are not yet injected automatically into CAPI bootstrap templates or executed on newly provisioned physical hosts, so Worker host synchronization remains unverified.

## Required contexts and image references

The deployment script requires three distinct kubeconfig contexts:

- `CLOUD_CONTEXT`
- `EPH_CONTEXT`
- `TARGET_CONTEXT`

All images must be pinned by immutable SHA-256 digest:

- `CLOUD_CONTROL_IMAGE`
- `EPH_AGENT_IMAGE`
- `TARGET_AGENT_IMAGE`

Run `deploy.sh` only after the secrets below have been provisioned by your secret manager. The script performs server-side dry-run validation before applying resources and waits for all three Deployments.

## Required Secrets

### Cloud management cluster

- `cloud-control-agent-database`: key `database-url` (PostgreSQL connection string).
- `cloud-control-agent-mtls`: keys `tls.crt`, `tls.key`, `client-ca.crt`.

The Cloud Control API listens on TCP/8443 and is exposed as a LoadBalancer Service. Configure the cloud provider's load-balancer policy and DNS so EPH and Target can reach it over HTTPS. The server requires client certificates. Use a TCP/L4 load balancer with TLS passthrough; a TLS-terminating load balancer would break end-to-end mTLS unless it is explicitly configured to re-authenticate and forward the verified client identity through a separate trusted mechanism.

### EPH cluster

- `eph-agent-credentials`: `control-url`, `agent-id`, `installation-id`.
- `agent-control-plane-mtls`: `ca.crt`, `tls.crt`, `tls.key`.
- `capi-pivot-source-kubeconfig`: key `kubeconfig` (required at EPH bootstrap).
- `capi-pivot-target-kubeconfig`: key `kubeconfig` (optional until the Target Control Plane API is ready; mount it later before enabling the Pivot adapter).

The EPH client certificate Common Name must equal the configured Agent UUID. EPH identity must not include a Target cluster ID.

### Target cluster

- `target-agent-credentials`: `control-url`, `agent-id`, `installation-id`, `cluster-id`, `ironic-api-url`, `ironic-api-token`, `ironic-internal-ntp-ip`. The Ironic API endpoint must use HTTPS and its certificate must chain to a CA trusted by the Target Agent image. The NTP IP is a separately pinned internal service/VIP address; Target Agent rejects command payloads that request any other NTP server. The API token is loaded from the Kubernetes Secret and is never included in command payloads or logs.
- `agent-control-plane-mtls`: `ca.crt`, `tls.crt`, `tls.key`.

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

Use `build-agents.sh` with immutable release versions and verified upstream checksums. Current EPH and Target image builds are verified only for `linux/amd64`; the script rejects other platforms rather than silently producing an unverified image.

Example environment variables:

```bash
export IMAGE_TAG=2026.11.17-rc1
export CLOUD_CONTROL_IMAGE_REPOSITORY=registry.example.com/ai-agent-cloud-control-agent
export EPH_AGENT_IMAGE_REPOSITORY=registry.example.com/ai-agent-eph-agent
export TARGET_AGENT_IMAGE_REPOSITORY=registry.example.com/ai-agent-target-agent
export CLUSTERCTL_VERSION=v1.10.5
export CLUSTERCTL_SHA256_AMD64=<verified-64-character-sha256>
export KUBECTL_VERSION=v1.32.2
export KUBECTL_SHA256_AMD64=<verified-64-character-sha256>
bash build-agents.sh
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
bash deploy.sh
```

## Current implementation boundary

The current fresh runtimes provide mTLS identity, heartbeat, command claim/result transport, lease renewal, PostgreSQL-backed command and Target checkpoint persistence, compare-and-swap checkpoint updates, Target leader election, ordered handoff evidence validation, read-only CAPI/Metal3 observation, live TLS-authenticated Ironic node verification for every target BMH, and Worker NTP policy validation/command rendering. The EPH Agent now has a dry-run and approval-digest-gated `clusterctl move` adapter with provider-version and Target-master controller-placement checks. It has not yet been validated against the selected live provider compatibility matrix. Autonomous EPH cleanup, Worker provisioning, host-level chronyd application/synchronization, and end-to-end integration with the existing platform API are not yet enabled. They must be implemented as explicit adapters and verified with the selected CAPI/Metal3/Ironic provider versions before production use.
