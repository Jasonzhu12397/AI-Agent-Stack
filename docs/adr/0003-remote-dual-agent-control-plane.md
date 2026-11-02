# ADR 0003: Remote EPH and Target Cluster Agent Control

- Status: Accepted
- Date: 2026-11-02

## Context

Bare-metal Kubernetes delivery requires a temporary management/EPH environment before the target cluster exists. A target-side agent cannot be a prerequisite for target-cluster creation because it has no cluster in which to run yet. After provisioning, the target cluster needs its own agent for day-2 inspection and controlled operations.

## Decision

Use one central Remote Control Plane Agent and two separately enrolled node-agent roles:
- EPH Node Agent: preflight and management-cluster/CAPI/Metal3/Ironic coordination.
- Target Cluster Agent: installed after target API readiness; target-side health, diagnostics and scoped operations.

Both agents use outbound authenticated communication and durable, lease-protected commands. The central service routes typed commands to a specific enrolled node. High-risk mutation commands require approval bound to a canonical payload hash. Agent processes never accept arbitrary shell or arbitrary manifest commands.

CAPI, Metal3 and Ironic remain authoritative infrastructure controllers. The control plane verifies external postconditions before marking delivery successful.

## Consequences

- The control plane can orchestrate the full bootstrap lifecycle without requiring inbound access to EPH or target nodes.
- EPH and target credentials, ServiceAccounts, command allowlists and revocation are isolated.
- Target Agent enrollment is a post-provisioning stage, not an installation prerequisite.
- Real provisioning adapters require an explicit, tested Kubernetes/CAPI/Metal3/Ironic compatibility matrix.
- Initial bearer enrollment is suitable only for protected development bootstrap; production must use mTLS or short-lived workload identity.
