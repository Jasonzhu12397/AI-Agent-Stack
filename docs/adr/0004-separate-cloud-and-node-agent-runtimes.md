# ADR 0004: Separate Cloud and Kubernetes Agent Runtime Artifacts

- Status: Accepted
- Date: 2026-11-09

## Context

The platform has two distinct trust domains and execution environments. The cloud environment runs the durable AI orchestration runtime. EPH and Target Kubernetes clusters run node-local agents that need narrowly scoped Kubernetes and infrastructure API access. Packaging both runtimes into the same image would unnecessarily distribute cloud code and dependencies into physical clusters and blur their credential boundaries.

## Decision

Build and deploy three separate artifacts:

1. Studio API image (`Dockerfile`): Django/ASGI user API, authentication, tenant/project authorization, durable state, Agent registry, approvals and Agent command/heartbeat endpoints. It excludes the Node Agent source tree.
2. Cloud AI Agent image (`Dockerfile.cloud-agent`): the cloud task runtime (`worker_app.main`) for LLM/model routing, tools, RAG, workflow execution, task leases and orchestration. It includes the API/domain Python packages needed by Django models but excludes the Node Agent source tree.
3. Kubernetes Node Agent image (`Dockerfile.node-agent`): only `apps/node_agent`, PyYAML and checksum-verified kubectl/clusterctl binaries. It excludes Django, worker/API packages, cloud database clients and cloud model credentials.

EPH and Target Agents share the Node Agent image but use separate Kubernetes ServiceAccounts, enrollment identities, credentials, command allowlists and deployments. The cloud Agent and Node Agent communicate through the versioned Agent command/heartbeat API, authenticated by per-agent credentials and the configured mTLS ingress.

## Consequences

- Cloud LLM execution and durable orchestration stay in the cloud trust domain.
- Physical clusters receive only the small cluster-side runtime they need.
- API, cloud Agent and Node Agent can be built, scanned, versioned and rolled back independently.
- The Node Agent cannot access cloud task internals by importing cloud worker modules.
- The cloud Agent does not receive kubeconfigs, host mounts, BMC secrets or Kubernetes ServiceAccount tokens by default.
- CI must verify image source boundaries and compose wiring in addition to unit and integration tests.
