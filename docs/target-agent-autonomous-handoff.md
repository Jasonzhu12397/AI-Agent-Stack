# Target Agent autonomous EPH handoff contract

This contract supersedes the older EPH-as-a-future-worker/reclamation flow.

## Topology invariants

- EPH is exactly one temporary Single-Node Kubernetes cluster. There are no EPH Kubernetes workers.
- Target Control Plane masters are provisioned first.
- Target Agent runs on a Target Control Plane master and resumes the durable deployment checkpoint.
- BMH registry identity and controller ownership must be transferred and verified before EPH cleanup.
- Target Agent, not Cloud Control Agent, owns the handoff sequence. Cloud Control schedules work, stores durable state, relays events and observes progress; it does not approve Target adoption or EPH cleanup.
- EPH cleanup removes the temporary EPH Agent/bootstrap workload and revokes EPH-only credentials only after CAPI pivot, Target Agent readiness and BMH migration are verified.
- Target Agent then provisions all Target Worker nodes through CAPI/Metal3/Ironic.
- Chronyd installation/configuration/verification applies to Target Worker hosts only. It is not an EPH-node bootstrap task.

## Required ordered phases

`eph_single_node_ready -> target_control_plane_ready -> target_agent_ready -> capi_pivot_verified -> bmh_registry_migrated -> eph_cleanup_verified -> worker_provisioning -> workers_ready -> completed`

Each transition requires positive evidence from the preceding operation. Missing evidence, stale identity, duplicate BMH UID, or a mismatch between source and target BMH identity sets blocks the transition. The Target Agent must persist a checkpoint before and after every external side effect and resume idempotently after restart.

## BMH migration invariant

The source and target inventories must be complete and have exactly equal sets of (namespace, name and stable hardware identity). The target Metal3/CAPM3 controller must be healthy and demonstrably own/reconcile the migrated inventory. A matching count alone is insufficient. Do not delete source BMH objects until target ownership and reconciliation are verified.

## EPH cleanup invariant

EPH cleanup is not a Cloud approval operation. It is an autonomous Target Agent action after all of the following are true:

1. Target Agent is running on a Target Control Plane master and has restored the deployment checkpoint.
2. CAPI pivot has completed and target controllers are healthy.
3. Target Cluster UID and CAPI object ownership are preserved.
4. BMH source/target identity sets match and target Metal3 controller ownership is verified.
5. Required CAPI/Metal3 management components no longer depend on the EPH API.
6. Cleanup is idempotent, checkpointed, and revokes EPH-only credentials.

A failure at any gate leaves EPH intact and reports a recoverable blocked state.

## Worker and chronyd invariant

Only Target Agent provisions Target Workers. Each worker's host preparation includes chronyd installation, configuration, enablement and synchronization verification, followed by CAPI Machine/Node readiness and network postflight. No chronyd operation is scheduled against the EPH Single-Node cluster.

## Implementation boundary

The node-agent lifecycle policy module enforces topology, phase ordering, BMH identity equality, Target ownership evidence and chronyd role scope. These checks are policy and validation primitives; they do not claim to perform a live clusterctl move, delete EPH Pods, mutate BMH objects or install chronyd. Those external side effects remain behind versioned Kubernetes/CAPI/Metal3/host adapters and must be tested against the selected provider versions before production enablement.
