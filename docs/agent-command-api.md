# Agent Command History API

The Metal3 Fleet terminal uses a tenant/project-scoped aggregate endpoint instead of issuing one request per Agent node.

## Endpoint

`GET /api/v1/tenants/{tenant_id}/projects/{project_id}/agent-commands/`

Query parameters:

- `limit`: 1–200, default 100.
- `offset`: non-negative integer, default 0.
- `status`: optional AgentCommand status.
- `node_id`: optional node UUID within the tenant/project scope.
- `command_type`: optional exact command type.

The response includes `count`, `limit`, `offset` and the standard Agent command payload. Commands are ordered newest-first. Tenant membership and project membership are checked using the same access rules as Agent node inventory. The API never returns commands from another project.

The Fleet UI refreshes persisted commands every 15 seconds and labels the terminal as a polling view. It is not a WebSocket stream. Durable command logs are available from the command result and error records; streaming log transport is a separate implementation phase.

## Deployment-scoped CAPI Pivot simulation

The Supervisor creates `cluster.capi.pivot.execute` only after source and target preflight plus provider-version compatibility have passed. The command remains `WAITING_APPROVAL` until a tenant owner or administrator other than the requester approves its exact immutable `plan_hash`. The result handler independently revalidates the succeeded command state, approver identity, approval timestamp, approved plan hash and recomputed command payload hash before accepting a simulation result; a callback without that approval binding is rejected.

The Target Agent runs `clusterctl move --dry-run` and read-only source/target checks only. It must report `pivot_simulated=true`, `pivot_executed=false`, `ownership_transferred=false` and `side_effects=false`. A successful simulation does not mean the Target Agent owns the cluster: the deployment remains at `pivot_simulation_ready`, and target verification, worker provisioning and EPH reclaim are not automatically advanced. A real ownership transfer requires a separately designed and explicitly enabled execution adapter and approval policy.

## Deployment-scoped CAPI Worker provisioning

The Target Agent accepts `cluster.capi.worker.preflight` as a read-only inventory command. After the Supervisor validates the result, it creates `cluster.capi.worker.provision` itself and exposes its UUID at `deployment.plan_result.dual_agent.worker_provisioning_command_id`. Clients must not create this mutating command through `POST /api/v1/agent-nodes/{node_id}/commands/`; that endpoint explicitly rejects it so a caller cannot bypass the deployment ownership, preflight and inventory gates.

The generated command is stored as `WAITING_APPROVAL`. A tenant owner or administrator other than the requester must approve it through `POST /api/v1/agent-commands/{command_id}/approve/` with the exact immutable `plan_hash`. Approval expires after the standard short approval window. The Target Agent performs Kubernetes Server-Side Dry-Run only and returns a hashed MachineDeployment manifest preview. Real MachineDeployment writes are disabled in this runtime: the API helper rejects every request that is not `dryRun=All`, the command reports `execution_allowed=false`, `side_effects=false`, and zero created workers, and the Supervisor keeps the deployment in `plan_ready` with `worker_provisioning_simulated_manual_execution_required`. It does not advance the worker-pool checkpoint or queue postflight because no worker Machines or Nodes were created. A separate, explicitly enabled production execution adapter and its approval policy are required before any real worker provisioning can be performed.

## Mutating command lease expiry

A command that has entered `running` and whose lease expires is not automatically delivered again when it belongs to `MUTATING_COMMANDS`. The platform marks it failed with `MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED` (or the dedicated CAPI Pivot outcome code), clears the lease, and requires an operator to inspect the target cluster before creating a new command. This is intentional at-most-once dispatch for uncertain mutating outcomes; it prevents a restarted EPH or Target Pod from blindly repeating provisioning or ownership-related actions. Read-only commands may still be retried under the configured attempt limit.

The lease expiry does not prove that the original operation failed. The terminal state means the result is unknown and must be reconciled against observed Kubernetes/CAPI/Metal3 state before proceeding.


## Deployment checkpoint resume and worker provisioning

The control plane advances the Target Agent deployment checkpoint only after it validates the corresponding command result. A validated worker preflight creates or advances the durable checkpoint to `worker_provisioning`, with `pending_step=worker_provisioning`. The Target Agent restores checkpoints while the deployment is awaiting approval or provisioning, not only during the initial handoff.

Before dispatching `cluster.capi.worker.provision`, the Target Agent requires a matching deployment ID, cluster name, generation, checkpoint phase, sequence and pending step. A missing, stale, mismatched or already-completed checkpoint fails closed before the Kubernetes mutation is attempted. After the control plane validates worker readiness, it advances the checkpoint to `worker_pool_verified`; a validated final postflight advances it to `completed`. The control plane remains authoritative for approval and deployment lifecycle state. Checkpoints are evidence for safe resume decisions, not permission to bypass approval.

Checkpoint advancement is idempotent for duplicate result processing. EPH reclaim remains disabled and is not implied by a completed worker checkpoint.


Checkpoint phase progression is monotonic. The control plane rejects unknown phases, attempts to move a checkpoint backwards, and conflicting updates within the same phase. An exact replay of an already-applied phase update remains idempotent. A late or stale result therefore cannot roll a verified worker pool back into worker provisioning.
Target Agent checkpoint sequence numbers must be contiguous: each new checkpoint must use exactly the previous sequence plus one. Gaps are rejected with HTTP 409 and include the expected and received sequence numbers; exact duplicate replay remains idempotent, while lower sequence numbers remain stale and rejected.

## The authorized command surface is the implemented command surface

`packages/agent_contracts/agent_commands.py` is what the platform authorizes. It
had drifted from what the Agents execute: the terminal listed `cluster.plan`,
`cluster.provider.discover`, `cluster.manifest.validate` and `capi.cluster.observe`
for an EPH node, and several `cluster.*` pivot aliases for a Target node that no
handler answered, so dispatching them returned `ACTION_NOT_IMPLEMENTED`.

Two changes close that gap for good:

- **Read-only diagnostics execute.** `packages/agent_contracts/readonly_commands.py`
  implements `node.health`, `host.preflight`, `cluster.health`, `cluster.observe`,
  `cluster.diagnose`, `cluster.postflight`, `cluster.network.inspect`,
  `cluster.manifest.validate` and `k8s.resource.read` once, and both Agents call it
  through their own runner. `cluster.manifest.validate` sends the documents to
  `kubectl apply --dry-run=server -f -`, so the API server validates them without
  creating anything; `k8s.resource.read` is restricted to an allowlist of namespaced
  and cluster resources.
- **The registry only names implemented actions.** The stale `cluster.*` aliases
  were removed; orchestration keeps its canonical `eph.*` / `target.*` names, and
  `cluster.plan` / `capi.cluster.observe` are real EPH handlers (fleet planning and
  the CAPI observer). `tests/test_agent_command_coverage.py` dispatches every
  authorized action through the real `execute()` of both Agents with stubbed
  binaries: a dead command now fails the build instead of the operator.

`MUTATING_COMMANDS` follows the same rule: it lists the Target actions that really
mutate (`target.capi.pivot`, `target.worker.provision`, `target.eph.cleanup`) plus
`cluster.kubernetes.version.upgrade`, so an uncertain lease expiry is treated as an
unknown mutating outcome instead of a retryable read.

## Kubernetes minor upgrade has a simulation mode and an execution mode

`packages/agent_contracts/kubernetes_upgrade.py` owns the request rules (exact
payload keys, adjacent minor only, target minor inside the supported list, and
`execution_mode` in `simulation` or `execute`). The platform validates the request
with it and the Target Agent executes it with the same function.

* `simulation` returns the four ordered, unexecuted stages — `preflight`,
  `control_plane`, `workers`, `postflight` — plus `upgrade_simulated=true`,
  `upgrade_performed=false`, `requires_manual_execution=true`,
  `resume_supported=false` and `side_effects=false`.
* `execute` runs the sequence through `apps/target_agent/kubernetes_upgrade_exec.py`:
  `kubeadm upgrade plan`, `kubeadm upgrade apply -y`, then per worker
  `kubectl drain` → `ssh <KUBERNETES_UPGRADE_REMOTE_USER>@<InternalIP> kubeadm upgrade node`
  → `kubectl uncordon`, then a postflight that rejects any node still on the old
  minor. It reports `upgrade_simulated=false`, `upgrade_performed=true`,
  `requires_manual_execution=false`, `side_effects=true` and per-step exit codes.

Before `kubeadm upgrade plan`, the preflight stage checks the cluster facts an
upgrade depends on and records each one in `preflight.checks`: every node
`Ready=True`, no node cordoned, the `kube-system/kubeadm-config` version on the
same minor the cluster reports, every PodDisruptionBudget has disruptions
available, and `etcd.snapshot`: stacked etcd is backed up automatically. The
Agent runs `etcdctl snapshot save` inside the etcd pod on the local control
plane into `KUBERNETES_UPGRADE_ETCD_SNAPSHOT_DIR` (default
`/var/lib/etcd/backups`, the host data directory the pod mounts at the same
path, so the file survives a failed upgrade) and reads it back with
`etcdctl snapshot status` — a snapshot that cannot be read is not a backup. To
reuse an existing backup instead, set `KUBERNETES_UPGRADE_ETCD_SNAPSHOT`; it is
then accepted only while newer than
`KUBERNETES_UPGRADE_ETCD_SNAPSHOT_MAX_AGE_HOURS` (default 24). The path, whether
it was taken now and its verification are returned as `etcd_snapshot`. The
preflight writes only this backup file. Any failed check stops the run with
`UPGRADE_PREFLIGHT_BLOCKED` and the failing detail, before anything is drained.

Package upgrade is part of the same stages, not a manual preamble: set
`KUBERNETES_UPGRADE_PACKAGE_MANAGER` (`apt` or `dnf`) and
`KUBERNETES_UPGRADE_PACKAGE_VERSION` (the distribution package version, e.g.
`1.34.1-1.1` for apt or `1.34.1-1500` for dnf) and the agent installs
kubelet/kubeadm/kubectl before `kubeadm upgrade apply` on the control plane and,
through the remote executor, on each worker before `kubeadm upgrade node`, then
restarts kubelet. The version must be built from the target release; a version
from another minor answers `UPGRADE_PACKAGE_VERSION_MISMATCH` before anything is
drained. Without the two variables the run reports
`package_upgrade: skipped_unconfigured` and relies on the operator having the
binaries in place — the kubeadm steps then fail visibly instead of silently.

### Readiness probe

`cluster.kubernetes.upgrade.readiness` is a Target-only, read-only command. It
reports whether that Agent can run an execute upgrade: which of
`KUBERNETES_UPGRADE_PACKAGE_MANAGER`, `KUBERNETES_UPGRADE_PACKAGE_VERSION` and
`KUBERNETES_UPGRADE_REMOTE_USER` are present, whether `kubeadm`/`kubectl` are on
`PATH`, and the resulting `execute_ready` flag. It reports presence only — never
a configured value — and needs no approval. The deployment row shows a
"Check upgrade readiness" action and renders `missing` names, so an operator sees
the exact variable to set instead of a bare "not ready".

### Rolling control-plane upgrades

A multi control-plane cluster is upgraded the way kubeadm prescribes, inside the
`control_plane` stage: exactly one `kubeadm upgrade apply` runs on the control
plane this agent is deployed beside, and each remaining control plane then gets
its packages, `kubeadm upgrade node` and a kubelet restart over ssh, in name
order. The local control plane is identified by hostname or by
`KUBERNETES_UPGRADE_LOCAL_NODE`; when neither matches a control-plane node the
run answers `UPGRADE_LOCAL_NODE_UNKNOWN` and changes nothing, because running
`kubeadm upgrade node` on the wrong host still upgrades that host. A peer with
no InternalIP answers `UPGRADE_CONTROL_PLANE_PEER_ADDRESS_UNKNOWN`.

etcd quorum is guarded during the roll, not only before it: `etcd.quorum`
requires the cluster to be healthy at the start, and after the local control
plane and after every peer the agent waits up to
`ETCD_HEALTH_ATTEMPTS`×`ETCD_HEALTH_INTERVAL_SECONDS` (20 × 5s) re-reading
`etcdctl endpoint health --cluster`. If etcd does not come back healthy the run
stops with `UPGRADE_ETCD_QUORUM_AT_RISK` before the next node is touched, and
each probe is returned in `etcd_health`, so a stalled roll is visible as
`etcd.quorum/<node>` failing rather than as a later, unexplained failure.

Every node that will be reached over ssh must first pass `ssh.reachable/<node>`:
`ssh -o BatchMode=yes -o ConnectTimeout=5 <user>@<address> true` runs during the
preflight, because the local control plane is upgraded before the first remote
hop, and an ssh failure after that leaves the cluster half upgraded.

Nodes already reporting the target minor are skipped with a
`<node>: already on <version>, skipped` note, so a retry after a mid-run failure
does not repeat `kubeadm upgrade apply` or re-ssh into nodes that are done.
A retry of an upgrade that already finished changes nothing and says so:
every node already on the target minor is skipped (workers are not drained
again, `kubeadm upgrade apply` is not repeated), the run reports
`upgrade_performed=false` with `already_at_target=true`, and the platform
accepts that result only with that flag and an empty performed-step list. A run
that silently upgraded nothing is still refused.

Peer failures keep the node in the detail: `UPGRADE_CONTROL_PLANE_PEER_PACKAGE_FAILED`,
`UPGRADE_CONTROL_PLANE_PEER_FAILED`, `UPGRADE_CONTROL_PLANE_PEER_KUBELET_RESTART_FAILED`.

`execute` refuses what one agent cannot do safely: workers or control-plane
peers without `KUBERNETES_UPGRADE_REMOTE_USER`
(`UPGRADE_REMOTE_EXECUTOR_NOT_CONFIGURED`). It stops at the first failed step and
returns its exit code and output. Both modes still need a platform approval, and
the platform accepts execution evidence only when every stage ran to completion;
a retry that found every node already on the target reports
`upgrade_performed=false` with `already_at_target=true` instead.

## A failed upgrade is described, not repaired

`cluster.kubernetes.recovery.report` is read-only and needs no approval. It takes
an optional `target_version` (anything not shaped like `v1.34.1` is refused with
`RECOVERY_TARGET_VERSION_INVALID`) and answers with the state a failed upgrade
leaves behind: every node with its role and the kubelet version it actually
reports, whether that version is on target, the drift count, the health of each
stacked etcd member, and the snapshots already written to
`KUBERNETES_UPGRADE_ETCD_SNAPSHOT_DIR` (default `/var/lib/etcd/backups`). Nodes
it cannot read fail closed with `RECOVERY_NODES_UNREADABLE`; an unreadable etcd
or snapshot directory is reported as a warning instead of being guessed at, and a
cluster with external etcd reports `stacked=false, healthy=null` instead of
looking degraded.

A reused `KUBERNETES_UPGRADE_ETCD_SNAPSHOT` is verified the way a fresh one is:
the run reads it with `etcdctl snapshot status` inside the etcd pod, so a file
that exists but cannot be restored blocks the upgrade instead of passing on its
mtime.

## Rollback restores the snapshot and keeps the old data

`cluster.kubernetes.version.rollback` is the only automatic undo, and its scope is
narrow on purpose. It takes `cluster_name`, an absolute `snapshot_path` ending in
`.db` (no traversal), `execution_mode` and, for `execute`, `confirm_cluster_name`
that must equal `cluster_name`. Refusals are explicit:
`ROLLBACK_CONFIRMATION_MISMATCH`, `ROLLBACK_SNAPSHOT_PATH_INVALID`,
`ROLLBACK_CLUSTER_NAME_MISMATCH`, `ROLLBACK_EXECUTION_MODE_INVALID`.

A stacked etcd is restored member by member instead of being refused. Restoring
one member of a three-node cluster alone would break quorum, so the executor reads
`member list --write-out=json` and ties every member to its control-plane node
through the peer URL host and the etcd pod's `nodeName`. It then stages the
snapshot on each remote host (`ssh` + `scp` over
`KUBERNETES_UPGRADE_REMOTE_USER`), restores every member locally through
`kubectl exec` (the admin kubeconfig reaches all pods, the remote hosts do not need
`kubectl`), stops every kubelet before the first directory moves, swaps the
restored data in, and starts the local kubelet first so the quorum re-forms in a
known order. An unidentified local node or an unmatchable member blocks with
`ROLLBACK_PREFLIGHT_BLOCKED` (`ROLLBACK_ETCD_MEMBER_UNMATCHED` detail) before
anything is touched, and a missing remote executor reports
`ROLLBACK_REMOTE_EXECUTOR_NOT_CONFIGURED`.

`execute` is approval-gated like the upgrade, and it proceeds only when its
preflight passes: the node is Ready, the snapshot file exists, `etcdctl snapshot
status` returned its hash, and etcd is healthy (`ROLLBACK_PREFLIGHT_BLOCKED`
otherwise, with nothing touched). The steps are `etcd_restore` (restore into
`/var/lib/etcd/rollback-<utc>`), `data_swap` (stop the kubelet, move the live
member directory to `/var/lib/etcd/member.pre-rollback-<utc>`, move the restored
member in), `kubelet_restart` (start the kubelet and wait for the apiserver),
then `postflight` (nodes readable and etcd healthy). The previous data directory
is never deleted; if the swap fails the executor tries to move it back and the
result names where the data is kept.

Package downgrades are out of scope: kubeadm has no supported downgrade path, so
the binaries stay where they are and only control-plane state is restored.

Before anyone approves a rollback, `metal3_acceptance.py rollback` answers the
same question read-only on a real cluster: one control plane, the node Ready, the
snapshot readable from inside the etcd pod and its hash verifiable, etcd healthy.
It shells `kubectl` only, never `sudo`, and the lab workflow runs it when
`rollback_snapshot` is passed to the manual dispatch.

## Provisioning network defaults are derived, never borrowed

The Ironic template pre-fills `networking.interface` and `networking.ipAddress`
from the deployment profile. The address sources, in order:

1. `ip_address` — the address the EPH owns (`eph_network_addresses.provisioning`
   or `.control`), entered in the UI.
2. `ip_address_subnet` — the first free host of the provisioning subnet,
   skipping the segment's first usable host and every declared allocation pool.
3. `dhcp` — set to `true` when the provisioning network is DHCP-addressed; the
   `Ironic` CR then needs no static `ipAddress` (`IRONIC_IP_ADDRESS_REQUIRED`
   applies only to static links).

The subnet gateway is never used as the provisioning address: it belongs to the
router on that segment, so writing it into `networking.ipAddress` would put the
router's IP on the EPH NIC. If a static segment has no free host left, the
template reports `IRONIC_TEMPLATE_ADDRESS_UNAVAILABLE` and the deployment is
rejected until the operator supplies the address.

## VLAN bases follow what the segment carries

A VLAN segment that declares neither `bond` nor `interface` inherits a base by
role: `provisioning` and `control` stack on the **control** link (the reference
lab shape puts the PXE/provisioning VLAN 70 on `bond_control`), while `data`,
`storage-internal`, `storage-external` and `sriov` stack on the **data** bond.
A declared base always wins. Both the nmstate document and the Metal3
`networkData` use the same rule, so the Live ISO, the provisioning VLAN and the
control planes share one link stack.

Host link names may be `pci@0000:3b:00.0` and segment/bond names may carry
underscores (`bond_control`, `sdi_semc`). Kubernetes object names are derived
with `dns_name` (`sdi_semc` → `sdi-semc`) and that derived name is used
consistently for the IPPool, `ipAddressFromIPPool` and SR-IOV policy names.

A physical EPH has bonds and VLAN sub-interfaces, not bridges, so the host check
verifies declared links: `metal3_deploy.py verify --required-link bond_control
--required-link bond_control.70`.

## Verification reuses the recorded network profile

`plan --network-profile profile.yaml` renders the host `networkData` from that
profile and records it as `network-profile.yaml` in the output directory. `verify`
then derives the host links to check from the recorded profile instead of a
hand-written list, so the links that are verified are exactly the bonds and VLAN
interfaces cloud-init brings up (`--required-link` remains available to override).
