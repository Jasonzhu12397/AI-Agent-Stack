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
- Chronyd is mandatory on EPH, Target Control Plane masters and Target Worker hosts. Target Control Plane nodes synchronize from approved external NTP sources and provide the internal NTP service on the Ironic internal IP/VIP. Every Target Worker synchronizes only from that Ironic internal IP/VIP; workers must never be configured with external NTP sources. EPH uses its independent bootstrap NTP source because the Target Control Plane does not yet exist during EPH kubeadm initialization.

## Required ordered phases

`eph_single_node_ready -> target_control_plane_ready -> target_agent_ready -> capi_pivot_verified -> bmh_registry_migrated -> eph_cleanup_verified -> worker_provisioning -> workers_ready -> completed`

Each transition requires positive evidence from the preceding operation. Missing evidence, stale identity, duplicate stable BMH hardware identity, or a mismatch between source and target BMH identity sets blocks the transition. The Target Agent must persist a checkpoint before and after every external side effect and resume idempotently after restart.

## BMH migration invariant

The source and target inventories must be complete and have exactly equal sets of (namespace, name and stable hardware identity). The target Metal3/CAPM3 controller must be healthy and demonstrably own/reconcile the migrated inventory. A matching count alone is insufficient. Do not delete source BMH objects until target ownership and reconciliation are verified.

## EPH cleanup invariant

EPH cleanup is not a Cloud approval operation. It is an autonomous Target Agent action after all of the following are true:

1. Target Agent is running on a Target Control Plane master and has restored the deployment checkpoint.
2. CAPI pivot has completed and target controllers are healthy.
3. The source and target Kubernetes object UIDs are recorded separately. The migrated target CAPI Cluster must carry the source UID in the `ai-agent-stack.io/source-cluster-uid` annotation, preserve the stable logical cluster identity, and have verified Target ownership. Do not require source and target Kubernetes UIDs to be equal; Kubernetes assigns a new UID when the object is recreated in the target API server.
4. BMH source/target identity sets match and target Metal3 controller ownership is verified.
5. Required CAPI/Metal3 management components no longer depend on the EPH API.
6. Cleanup is idempotent, checkpointed, and revokes EPH-only credentials.

A failure at any gate leaves EPH intact and reports a recoverable blocked state.

## Hierarchical NTP and chronyd invariant

- EPH Single-Node: install/configure/enable chronyd and verify its independent bootstrap NTP source before `kubeadm init`. It must not depend on the not-yet-deployed Target Control Plane.
- Target Control Plane: synchronize chronyd from approved external NTP sources. Expose a real internal NTP service on the Ironic internal IP/VIP over UDP/123, reachable from the Worker provisioning network.
- Every Target Worker: install/configure/enable chronyd with exactly the Ironic internal IP/VIP as its NTP source. Do not configure public/external NTP pools or fallback sources on Workers. Verify reachability and synchronization before `kubeadm join`.
- Target Control Plane must not be considered ready to provision Workers until its internal NTP service is listening and reachable. Worker NTP verification must prove it is using the configured Ironic internal endpoint, not merely that the local clock appears synchronized.

Only Target Agent provisions Target Workers. Each worker's host preparation includes chronyd installation, configuration, enablement and synchronization verification, followed by CAPI Machine/Node readiness and network postflight.

## Implementation boundary

The new `apps/target_agent/handoff.py` module enforces topology, phase ordering, stable BMH hardware-identity equality and Target ownership evidence. The new `apps/target_agent/ntp_policy.py` module enforces Worker chronyd source scope. These checks are policy and validation primitives; they do not claim to perform a live clusterctl move, delete EPH Pods, mutate BMH objects or install chronyd. Those external side effects remain behind versioned Kubernetes/CAPI/Metal3/host adapters and must be tested against the selected provider versions before production enablement.


## Trusted NTP policy configuration

The Target Agent does not accept its approved external NTP sources or Worker network CIDRs
from a Cloud Control command payload. These values are deployment policy and must be supplied
through the `target-agent-credentials` Secret:

- `approved-external-ntp-sources`: comma-separated approved upstream NTP IPs or DNS names.
- `worker-network-cidrs`: comma-separated CIDRs permitted to query the Control Plane NTP service.

The Target Agent validates every Control Plane bootstrap configuration against these trusted
values. A command payload cannot approve its own NTP source or broaden the served Worker networks.
Missing policy keys block NTP attestation and rendering; they must not be replaced with public
fallback pools or caller-provided values.
