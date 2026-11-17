# Three-Agent deployment

This directory deploys the new three-Agent architecture. The legacy Node Agent deployment path is not used by these manifests or scripts.

## Runtime topology

- **Cloud Control Agent**: cloud-side durable command coordinator and mTLS API. Deploy in the Alibaba Cloud or Tencent Cloud management environment.
- **EPH Agent**: one replica inside the temporary EPH Single-Node Kubernetes cluster. It performs source-side preflight and reports readiness. Mutating CAPI Pivot remains blocked until its dedicated, approved adapter is implemented and tested.
- **Target Agent**: three replicas, spread one per Target Control Plane master. Kubernetes Lease leader election ensures only the elected replica polls and executes commands. All replicas expose readiness and report their own pod identity.

The Target Agent currently has read-only Kubernetes/CAPI/Metal3 observation handlers and an explicit Worker NTP policy validator. It does not yet provision Workers or configure chronyd on hosts. The validator rejects external NTP sources and permits only the Ironic internal IP/VIP; this is a policy gate, not proof of host synchronization.

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

- `target-agent-credentials`: `control-url`, `agent-id`, `installation-id`, `cluster-id`.
- `agent-control-plane-mtls`: `ca.crt`, `tls.crt`, `tls.key`.

All three Target replicas share one logical Agent UUID and installation/cluster identity. Their per-pod `AGENT_INSTANCE_ID` is populated from the Pod name. The Target client certificate Common Name must equal the logical Agent UUID.

## Cloud Control database provisioning

The Cloud Control Agent creates its tables on startup. Before Agents can connect, register their identities in `agent_control_agents` and create a deployment record in `agent_control_deployments`. The Agent UUID, role, installation UUID and (for Target only) cluster UUID must match the mTLS certificate and Agent environment.

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

The current fresh runtimes provide mTLS identity, heartbeat, command claim/result transport, Cloud Control durable command storage, Target leader election, read-only CAPI/Metal3 observation and Worker NTP policy validation. CAPI Pivot execution, BMH ownership transfer, autonomous EPH cleanup, Worker provisioning, host-level chronyd installation/synchronization, and end-to-end integration with the existing platform API are not yet enabled. They must be implemented as explicit adapters and verified with the selected CAPI/Metal3/Ironic provider versions before production use.
