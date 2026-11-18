# ADR 0004: Separate Cloud and Kubernetes Agent Runtime Artifacts

- Status: Superseded by [ADR 0005](0005-three-agent-fresh-runtime.md)
- Date: 2026-11-09

## Historical record

This ADR records the earlier Cloud + shared Node Agent packaging proposal. It is retained only to explain the evolution of the architecture. It is not an implementation guide and must not be used to create new code, images, deployments, tests, or compatibility adapters.

The shared `apps/node_agent` runtime, `Dockerfile.node-agent`, Node Agent image, and Node Agent deployment path are retired and removed from the fresh implementation branch.

## Current authoritative decision

[ADR 0005: Fresh Three-Agent Runtime](0005-three-agent-fresh-runtime.md) is the sole authoritative architecture:

1. **Cloud Control Agent** runs in the Alibaba Cloud or Tencent Cloud management environment. It owns durable command/deployment state, approvals, dispatch, lease coordination and status aggregation.
2. **EPH Agent** runs as one Pod in the temporary EPH single-node Kubernetes environment. It owns source-side preflight, bootstrap checks and the source-side Cluster/BMH identity evidence; it does not perform the CAPI Pivot or the Target takeover.
3. **Target Agent** runs as three replicas on the target Kubernetes Control Plane masters. Kubernetes Lease leader election ensures one active executor. It owns target adoption, the CAPI Pivot (`clusterctl move`), CAPI/Metal3/Ironic observation and verification, BMH ownership handoff, EPH cleanup authorization and Target Worker provisioning.

These are independent runtimes with independent entrypoints, image builds, identity scopes, credentials, Kubernetes ServiceAccounts and capability allowlists. There is no per-Worker Agent, shared Node Agent runtime, or Network Inventory Agent DaemonSet.

## Migration rule

Do not restore or import the retired runtime to satisfy a failing test. Replace old tests with behavior tests against the owning fresh Agent. If a capability is not implemented by its owning Agent, it must fail closed and be tracked as incomplete rather than routed through legacy code.
