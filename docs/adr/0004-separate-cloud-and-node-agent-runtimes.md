# ADR 0004: Separate Cloud and Site-Side Agent Runtime Artifacts

- Status: Accepted
- Date: 2026-11-09

## Context

The platform has three logical Agent roles across two trust domains:

1. Cloud Control Agent in Alibaba Cloud or Tencent Cloud.
2. EPH Agent in the temporary RAM-booted EPH Live OS bootstrap Kubernetes cluster.
3. Target Agent as one cluster-scoped service with three Pod replicas on the target Kubernetes control-plane/master nodes.

There is no separate Node Agent role and no per-worker resident Agent/DaemonSet. EPH and Target are different roles with different command allowlists and credentials, but they share a small site-side runtime implementation and image. Packaging the cloud runtime into site clusters would unnecessarily distribute cloud code and dependencies and blur credential boundaries.

## Decision

Build and deploy three separate artifacts:

1. Studio API image (`Dockerfile`): Django/ASGI user API, authentication, tenant/project authorization, durable state, Agent registry, approvals and Agent command/heartbeat endpoints. It excludes the site-side Agent runtime.
2. Cloud Control Agent image (`Dockerfile.cloud-agent`): the cloud task runtime (`worker_app.main`) for LLM/model routing, tools, RAG, workflow execution, task leases and orchestration. It includes the API/domain Python packages needed by Django models but excludes the site-side Agent runtime.
3. Shared EPH/Target site runtime image: only the site-side Agent implementation, PyYAML and checksum-verified kubectl/clusterctl binaries. It excludes Django, worker/API packages, cloud database clients and cloud model credentials.

The EPH Agent is deployed as one Pod in the temporary EPH bootstrap cluster and uses a dedicated EPH enrollment identity and command allowlist. The Target Agent is deployed as three Pods across the three target masters; all replicas share one cluster-scoped Target identity and Kubernetes Lease leader election ensures only one active poller/executor. Standby replicas do not poll or submit command results. There is no fourth Node Agent role.

The cloud Control Agent and the two site-side Agent roles communicate through the versioned Agent command/heartbeat API. Each site-side role has separate credentials and authorization. The Target Agent's three replicas share the same logical identity by design; they are not independently enrolled hosts.

## Consequences

- Cloud LLM execution and durable orchestration stay in the cloud trust domain.
- Physical clusters receive only the site-side runtime they need.
- API, Cloud Control Agent and site-side runtime can be built, scanned, versioned and rolled back independently.
- EPH and Target command allowlists and credentials remain separate even though they share a runtime image.
- Target Agent command dispatch is single-active and protected by Kubernetes Lease leadership.
- The site-side runtime cannot access cloud task internals by importing cloud worker modules.
- The cloud Control Agent does not receive kubeconfigs, host mounts, BMC secrets or Kubernetes ServiceAccount tokens by default.
- CI must verify image source boundaries, role allowlists, leader-election fencing and deployment topology in addition to unit and integration tests.

## Implementation naming note

Some existing source paths and test names still contain the historical `node_agent` label. Those are legacy implementation identifiers only and must not be interpreted as a deployed Agent role. New architecture, API/UI labels and deployment documentation must use Cloud Control Agent, EPH Agent and Target Agent.
