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

`eph_single_node_ready -> control_stack_ready -> bmh_registered -> bmh_ready -> target_control_plane_applied -> target_control_plane_ready -> target_agent_ready -> capi_pivot_verified -> bmh_registry_migrated -> eph_cleanup_verified -> worker_provisioning -> workers_ready -> network_ready -> completed`

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

## Network evidence gate before the Target handoff

The platform binds every deployment to the exact `AgentMachine` inventory revision
its network readiness gate approved, as `spec.network_evidence`. The binding is
re-verified inside the control plane when `target.handoff.evaluate` is enqueued,
before any command row exists:

- No bound evidence while a profile is present -> `NETWORK_EVIDENCE_MISSING`.
- `inventory_revision` or `observed_at` changed on any bound machine ->
  `NETWORK_EVIDENCE_DRIFT`. Re-collecting the fleet invalidates an in-flight plan
  by design; create a new deployment after collection.
- The dispatch payload always carries the same evidence the platform persisted,
  so the Target Agent cannot hand off against an unverified fleet.

The platform also exposes the same check as
`POST /api/v1/cluster-deployments/<id>/network/evidence/verify/`, which returns
`200 current`, `409 drift` or `409 missing` for automation outside the control
plane.

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


## Source-cluster and controller post-pivot proof

Target Agent must receive a temporary, read-only source-cluster observer kubeconfig in the
`target-agent-source-observer` Secret. It queries the source API directly and accepts source
absence only when the API returns Kubernetes NotFound for the exact Cluster object. API
unavailability, authentication failure, timeout, and any other error are not treated as absence.

The `target-agent-credentials` Secret must also contain
`required-controller-deployments`, a comma-separated allowlist of exact
`namespace/deployment-name` references for the selected CAPI, bootstrap, control-plane,
Metal3 and Ironic controllers. Target Agent checks each deployment's observed generation,
updated replicas and available replicas against its desired replica count before accepting
the pivot phase. A command payload cannot choose or weaken this controller policy.

The source observer credential is temporary and read-only. It remains available only until
the Target Agent has independently verified pivot and BMH ownership; the EPH cleanup executor
must revoke it as part of the credential retirement phase.


## Source-side BMH hardware provenance

The Target Agent's successful `target.capi.pivot` result carries the source-side BMH
identity set observed by the EPH source observer; this set is the only provenance used by
Target Agent ownership verification. Cloud Control validates the pivot result under the
Target agent's authenticated command lease, checks both source and target BMH digests, and
persists the resulting attestation (the `eph_pivot_attestation` trusted-context record) in a
dedicated deployment `trusted_context` field. This context is separate from Target Agent's
mutable phase checkpoint state.

When Target Agent claims a command, Cloud Control exposes this trusted context separately
from the command payload. Target Agent recomputes the source BMH digest, checks the adopted
Cluster's source-UID annotation, checks the target Cluster UID, and compares the source
identity set with the live target BMH/Metal3/Ironic inventory. A caller-supplied
`source_bmh_identities` payload field is never accepted as provenance. Missing, malformed,
replayed or digest-mismatched pivot evidence blocks BMH registry migration.

The Cloud Control database creates and upgrades the `trusted_context` JSONB column
independently of the mutable checkpoint `state` column. Only target-role claims receive
the EPH pivot attestation; the EPH agent cannot read target deployment context through
command claims.

## Handoff evidence semantics

A Target Agent handoff phase is accepted only from live observations or a fail-closed bootstrap
execution gate. In particular:

- `source_cluster_absent` is produced only after the source API returns NotFound for the
  exact Cluster object; transport and authentication errors are not absence.
- `target_controllers_ready` is produced only after every deployment in the deployment-level
  required-controller allowlist reports observed generation and all desired replicas updated
  and available.
- `worker_ntp_bootstrap_sources` records the exact NTP source in the Worker KubeadmConfig
  pre-kubeadm script whose synchronization gate had to exit successfully before kubeadm join.
  It is not a claim of continuous chronyd telemetry after boot.
- Approved external NTP sources and Worker network CIDRs come from the Target Agent's
  deployment Secret, never from command payload fields.

These distinctions are intentional: a positive configuration assertion must not be reported
as a live runtime observation.


## Executable phases and their guards

Worker provisioning is now an executable Target Agent action. It server-side
applies the KubeadmConfigTemplate, Metal3DataTemplate, Metal3MachineTemplate and
MachineDeployment, refuses to scale down without a drain executor, verifies BMH
capacity before admitting new replicas, and waits for the MachineDeployment to
report the requested replicas. EPH cleanup is executable through the attested
source-cluster observer credential and deletes only resources carrying the
cleanup annotation, the managed-by label and the exact deployment-id label. The
`completed` phase is accepted only after Target postflight and Worker network
postflight observe the live target cluster; caller-supplied success booleans are
never accepted.

Before the first write of `target.worker.provision` or `target.network.apply`,
the Target Agent runs `kubectl auth can-i` for every permission those actions
need. A missing rule returns `TARGET_RBAC_INSUFFICIENT:<verb>:<resource>` with
zero side effects, and a kubectl transport failure fails closed instead of being
treated as an allow. The required namespaced rules are in the
`target-agent-metal3-operator` Role and the cluster-scoped StorageClass rule is
in `target-agent-storageclass-writer`. The agent never receives wildcard verbs
or resources.

Operator bundles (Rook-Ceph and the SR-IOV Network Operator) remain a platform
preinstall step. Their upstream manifests create cluster-scoped RBAC and CRDs,
which are deliberately outside the Target Agent ServiceAccount's authority; the
agent only creates the namespaced custom resources and StorageClasses after the
operators are present.
