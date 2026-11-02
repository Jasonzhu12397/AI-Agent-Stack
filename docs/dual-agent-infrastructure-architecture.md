# Dual-Agent Infrastructure Control Architecture

## Purpose

This document extends the general AI Agent Studio architecture with a remotely managed, two-sided Kubernetes bare-metal delivery model. It does not replace the platform's general Agent, Workflow, Plugin, MCP, Skills or RAG capabilities.

## Components and trust boundaries

1. **Remote Control Plane Agent** runs in the central AI Agent Studio runtime. It receives the operator's request, creates a typed plan, checks tenant/project scope, resolves the target EPH node and target cluster, obtains required approvals, dispatches commands and aggregates progress. It is not a privileged shell on either node.
2. **EPH Node Agent** is installed on the temporary EPH/management node. It establishes an outbound authenticated connection to the control plane, reports capabilities and health, performs host preflight, talks to the configured management Kubernetes API and coordinates CAPI/Metal3/Ironic provisioning. Its Kubernetes identity is scoped to the management cluster and required namespaces/resources.
3. **Target Cluster Agent** is bootstrapped only after the target cluster API is reachable. It connects outbound to the same control plane, reports target-cluster health and executes only explicitly granted, typed target-side operations using a dedicated ServiceAccount.
4. **CAPI / Metal3 / Ironic / IPA / BMC** remain the infrastructure control chain. AI agents submit validated declarative plans through adapters; they do not replace the controllers or treat an API create response as proof that provisioning succeeded.

## Bootstrap lifecycle

```text
Operator
  -> Remote Control Plane Agent
  -> EPH Node Agent (enroll, heartbeat, preflight)
  -> management Kubernetes API
  -> CAPI Cluster / MachineDeployment / Metal3 resources
  -> Metal3 controllers -> Ironic -> IPA/BMC -> physical nodes
  -> observe CAPI/Metal3 Conditions and Events until postconditions hold
  -> target cluster API becomes reachable
  -> bootstrap Target Cluster Agent with a short-lived, scoped enrollment
  -> Target Cluster Agent enrolls and reports cluster health
  -> remote control plane coordinates both agents for day-2 operations
```

The Target Cluster Agent is not a prerequisite for creating the target cluster. Before bootstrap it is shown as `NOT_ENROLLED`; after the API endpoint and bootstrap credentials are verified, the EPH-side bootstrap operation installs it. The control plane must not claim cluster delivery succeeded until the target-side postflight checks pass.

## Agent communication

- Both node agents initiate outbound HTTPS requests; no inbound SSH port or public agent listener is required.
- Enrollment credentials are one-time and scoped to one tenant/project/node type. The initial implementation stores only a SHA-256 hash of the issued bearer token and returns the raw token once.
- Production deployment must use mTLS workload identity (or an equivalent short-lived certificate identity), credential rotation and revocation. The bearer-token transport is a development bootstrap mechanism and must be protected by TLS.
- Heartbeat reports agent version, node type, capabilities, current cluster identity and timestamp.
- Commands are durable PostgreSQL records with idempotency key, status, attempts, lease token, lease expiry, result and error summary. A node can only claim commands addressed to itself.
- Command acknowledgements require both node authentication and the current lease token. Stale workers cannot overwrite a newer lease's result.
- The initial command contract is typed and allowlisted. Arbitrary shell, arbitrary Python, arbitrary URLs, arbitrary kubeconfig, and raw manifest execution are rejected.
- Mutating operations such as cluster provisioning and target-agent bootstrap are created in `WAITING_APPROVAL`. Approval binds the canonical payload SHA-256, requires a different tenant administrator and expires after 15 minutes. The command becomes dispatchable only when the approved hash still matches and the approval is fresh.

## Typed command contract

EPH-side command families:
- `node.health`
- `host.preflight`
- `cluster.plan`
- `cluster.provision` (approval-gated)
- `target-agent.bootstrap` (approval-gated)
- `capi.cluster.observe`
- `metal3.host.inspect`

Target-cluster command families:
- `node.health`
- `cluster.health`
- `cluster.observe`
- `cluster.diagnose`
- `cluster.postflight`
- `k8s.resource.read`

A command schema validates the payload before it enters the queue and the node agent validates it again before dispatch. Capability declarations do not grant permissions; effective access is the intersection of node identity, project grants, command allowlist, approval and active policy.

## Cluster delivery state model

`REQUESTED -> PREFLIGHT -> PLAN_READY -> WAITING_APPROVAL -> PROVISIONING -> CONTROL_PLANE_READY -> WORKERS_READY -> TARGET_AGENT_BOOTSTRAPPED -> POSTFLIGHT -> SUCCEEDED`

Any step may fail with a stable error code and diagnostic reference. A retry is allowed only when the operation is idempotent or the adapter can reconcile the external state. A timeout does not imply that an external provisioning operation did not happen. Deletion, BMC power control, disk wipe and production network changes require separate critical-risk policies and explicit approvals.

## Version and compatibility policy

The EPH Agent must report management-cluster Kubernetes version, CAPI provider versions, Metal3/Ironic versions, installed CRD API versions and enabled capabilities. The target cluster agent reports target Kubernetes version and supported API resources. The control plane selects only adapters whose compatibility matrix has been verified. No CAPI/Metal3/Ironic API version, BMH field or vendor-specific Redfish behavior is guessed.

Before enabling real provisioning, the operator must configure and verify:
- management cluster endpoint and CA;
- exact Kubernetes, CAPI core/provider, Metal3, Ironic and IPA versions;
- supported CRD schemas and template source;
- image, network, IPAM, DNS and BMC credential references;
- allowed namespaces, resource names, verbs and cluster/project scope;
- rollback/recovery and postcondition rules.

## Current implementation boundary

The first code slice introduces node enrollment, heartbeat, durable command dispatch, lease-protected results and hash-bound approval. The bundled node-agent runner supports safe health/preflight/plan commands. Actual CAPI/Metal3 resource writes, Ironic/BMC power operations, target-agent installation and target-cluster postflight are adapter work and remain disabled until a concrete environment compatibility matrix and credentials are supplied and tested.

## Kubernetes agent deployment

Kubernetes templates are under `deploy/agents/`:
- `namespace.yaml`: shared `ai-agent-system` namespace;
- `rbac.yaml`: separate EPH and target ServiceAccounts with read-only CAPI/Metal3 or target workload inspection scopes;
- `eph-node-agent.yaml`: EPH agent Deployment in the management/EPH cluster;
- `target-cluster-agent.yaml`: target agent Deployment, applied only after target API readiness;
- `README.md`: image, one-time token Secret and rollout instructions.

Both Deployments run non-root, drop Linux capabilities, disable privilege escalation, use a read-only root filesystem and mount only a small writable `/tmp` volume for liveness/readiness timestamps. The target workload Role is scoped to the `default` namespace as an example; create additional namespace-local RoleBindings only for approved namespaces. No Service or inbound listener is exposed.

## Cluster plan input contract

The EPH `cluster.plan` command validates the basic cluster identity, Kubernetes version, odd control-plane quorum and worker count. It also reports missing production prerequisites:
- exact CAPI core, Kubeadm bootstrap/control-plane, Metal3 infrastructure, Metal3, Ironic and IPA versions;
- Pod/Service CIDRs and control-plane endpoint;
- machine image URL and checksum;
- Metal3 namespace and enough named BareMetalHost references for all requested replicas;
- BMC credential references, never plaintext BMC credentials.

A complete input checklist is not a compatibility pass. `provisioning_ready` means required fields are present; `adapter_enabled` remains false until the exact target CRD schemas and provider versions are tested and the provisioning adapter is implemented. No manifest is applied by this plan command.
