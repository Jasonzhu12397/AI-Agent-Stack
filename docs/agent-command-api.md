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

Nodes already at or above the target are skipped with a
`<node>: already on <version>, skipped` note, so a retry after a mid-run failure
does not repeat `kubeadm upgrade apply` or re-ssh into nodes that are done. The
comparison is on the full `major.minor.patch` triple, not on the minor alone: a
node on `v1.34.0` is not "already on" `v1.34.3`, because the kubeadm patch it
still needs would otherwise never be installed while the run reported success.
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
mtime. The same rule as the rollback decides which paths that probe can reach:
the etcd static pod mounts `/var/lib/etcd` only, so a supplied snapshot stored
elsewhere is refused in preflight with the path it should be moved to, instead of
failing inside the pod with a bare "no such file".

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
through the peer URL host and the etcd pod's `nodeName`. A bracketed IPv6 peer URL
(`https://[fd00::1]:2380`) resolves to the node's `fd00::1` instead of a truncated
string. It then stages the snapshot on each remote host over
`KUBERNETES_UPGRADE_REMOTE_USER`: `scp` as the login user into `/tmp`, then
`sudo install -m 0444` into the root-owned etcd path and a readability probe, so
the bytes never half-land in a directory the login user cannot write. Every
member is restored locally through `kubectl exec` (the admin kubeconfig reaches
all pods, the remote hosts do not need `kubectl`), every kubelet stops before the
first directory move, the restored data is swapped in, and the local kubelet
starts first so the quorum re-forms in a known order.

Every remote hop is proven twice in preflight: `ssh.reachable` answers, and
`ssh.sudo` shows `sudo -n true` works, because a sudoers entry that asks for a
password returns immediately under `BatchMode=yes` and would strand a cluster
whose kubelets were already stopped. The same pair guards the upgrade path, so
neither operation starts on a host it cannot finish on.

Two preflight facts keep the restore from starting something it cannot finish.
`rollback.snapshot.path` requires the snapshot to sit under `/var/lib/etcd`, the
only host path the etcd static pod mounts - a file under `/var/backups` exists on
the host and is invisible inside the pod, so it is refused before a kubelet stops.
The snapshot verdict (`etcdctl snapshot status`) is taken once, on the member
whose host holds the file; a remote member is not asked to read a path that only
exists on this host. An unidentified local node or an unmatchable member blocks with
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

A rollback that fails part way does not leave the cluster down. Every failure
path - the kubelet refusing to stop, the directory moves, a start that does not
come up, an apiserver that never answers - runs the same recovery: previously
swapped directories are moved back, and every member whose kubelet was stopped
is started again, local members first. The result then carries `recovery` with
`members_restored`, `kubelets_started`, `kubelets_not_started`, `api_reachable`
and a `next_step` that names what a human still has to do, so a failed attempt
either lands on the pre-rollback data with a running kubelet or states exactly
which node is still down.

Before anyone approves a rollback, `metal3_acceptance.py rollback` answers the
same question read-only on a real cluster: one control plane, the node Ready, the
snapshot readable from inside the etcd pod and its hash verifiable, etcd healthy.
It shells `kubectl` only, never `sudo`, and the lab workflow runs it when
`rollback_snapshot` is passed to the manual dispatch.

The evidence file it writes (`evidence/rollback.json`, uploaded with the rest of
`evidence/`) carries `members` for a stacked etcd so the approver sees which node
and peer URL each restore targets, and `steps` with the same five stages and
commands the executor would run. The approval decision is therefore made against
the mutations themselves, not only against a pass/fail summary.

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

## etcd backups run unattended, and pruning is part of the backup

`cluster.kubernetes.backup.run` takes an `etcdctl snapshot save` of the etcd
static Pod into `/var/lib/etcd/backups/<cluster_name>-<yyyyMMddTHHMMSSZ>.db`. It
writes to `<name>.partial` first and only renames after `snapshot status`
verifies the file, so a listing never shows a truncated backup. A failed verify
removes the partial file and stops before retention, which means a broken backup
never causes an old good one to be deleted.

Retention is expressed in the request, not in a config file, so the scheduler is
the single place that decides policy: `keep_count` (default 12, floor 2, ceiling
168), `keep_max_age_hours` (default 8760, i.e. count-only unless a caller asks
otherwise) and `max_age_hours` (default 26, used only to flag staleness). The
pruner never deletes the newest file, never deletes a file younger than
`MIN_AGE_HOURS` (1) and never deletes a file in the caller's `protect` set, so a
snapshot a rollback is holding stays put.

`execute` requires `confirm_cluster_name` echoing `cluster_name` and needs no
human approval: a backup that waits for a person is not a backup. It is still a
mutating command, so it takes a lease and runs at most once.
`cluster.kubernetes.backup.status` is read-only and answers with the newest
backup name, its age in hours, the prunable set and a `ETCD_BACKUP_STALE` /
`ETCD_BACKUP_MISSING` finding, which is what a monitoring check should poll.

Nothing calls the run command by hand. `queue_etcd_backups` walks every
`ClusterDeployment` in `succeeded` with a Target Agent and queues one command per
hour bucket, idempotent per cluster, skipping clusters whose spec carries
`kubernetes.etcd_backup: false`. `deploy/docker-compose.yml` and
`deploy/docker-compose.cloud.yml` ship an `etcd-backup-scheduler` service that
runs it every `ETCD_BACKUP_INTERVAL_SECONDS` (default 3600). An external cron may
run the same command instead; the hour bucket keeps duplicate runs harmless.

`GET /api/v1/cluster-deployments/<deployment_id>/etcd-backup/` answers the same
question from the platform side without touching the cluster: the policy this
deployment declares (`kubernetes.etcd_backup`, `etcd_backup_keep_count`,
`etcd_backup_keep_max_age_hours`, `etcd_backup_max_age_hours`,
`etcd_backup_directory`, all validated by the backup contract), the newest
reported snapshot and its age, whether it is stale, the prunable set and the
findings. A deployment with `kubernetes.etcd_backup: false` reports that the
scheduler will not queue anything; a policy the contract refuses is reported as
`valid: false` with the contract error code, so an operator sees a bad
`etcd_backup_keep_count: 1` at config time instead of at 3 a.m.

Pre-upgrade snapshots live in the same directory, and they are a second kind of
file with a second duty: `snapshot-pre-<cluster>-<version>-<stamp>.db` is what an
undo of the last change restores. Files written by older versions
(`snapshot-pre-<version>-<stamp>.db`) are still recognised — they cannot name
their cluster, but the directory belongs to a single Target node, so they are
this cluster's files. They are kept by `pre_upgrade_keep_max_age_hours` (default
168h, i.e. a week) and never by count; the newest pre-upgrade snapshot is never
deleted regardless of age, because a rollback of the most recent change needs it.
Recurring freshness is judged only on periodic backups: a pre-upgrade snapshot
alone reports `ETCD_BACKUP_MISSING`, not a fresh backup set.

Retention is safe by construction only if it knows what is being held. The
scheduler reads the snapshot a rollback of this cluster still refers to (any
rollback command that did not fail or get cancelled), passes its bare file name
as `protect`, and the contract refuses anything that is not a bare snapshot name
of this cluster — a caller cannot point the pruning pass at a path, and cannot
protect a file that does not belong here. The read-only view reports the same
set, so "why was this file kept?" has an answer on the screen.

A backup that nobody checks is not a control. `python
apps/platform-api/manage.py check_etcd_backups` walks every `succeeded` deployment,
asks each one the same question the read-only view asks, writes an
`cluster.etcd_backup.problem` audit event for every cluster that has no usable
recent backup (with the contract code, the newest file and its age), prints the
whole picture as JSON and exits non-zero. `--dry-run` reports without writing
audit events; `--no-audit` keeps the exit code but stays out of the chain. A
cluster that declares `kubernetes.etcd_backup: false` reports
`ETCD_BACKUP_DISABLED` and is neither a problem nor an audit event, so a
deliberate opt-out does not page anyone.

`POST /api/v1/cluster-deployments/<deployment_id>/etcd-backup/policy/` writes the
policy into `spec.kubernetes` after the contract accepts it, so the UI cannot
save something the executor would reject: a bad `keep_count` comes back as
`ETCD_BACKUP_RETENTION_INVALID` and nothing is written. Only the policy fields
(`keep_count`, `keep_max_age_hours`, `pre_upgrade_keep_max_age_hours`,
`max_age_hours`, `directory`, `enabled`) are accepted; any other key is refused
with `policy_fields_only` rather than silently dropped, and every accepted change
is audited as `cluster.etcd_backup.policy.updated`.

A snapshot has to fit next to the database it copies. Between `prepare` and
`snapshot_save` the run measures the live database with `etcdctl endpoint status`
and the free space of the backup directory with `df -PB1`, and refuses to write
anything when `dbSize + min_free_bytes` does not fit
(`ETCD_BACKUP_DISK_LOW`). `min_free_bytes` defaults to 512MiB, is settable per
cluster as `kubernetes.etcd_backup_min_free_bytes` and can be lowered to 0 to
measure only. If either measurement cannot be read, the run fails with
`ETCD_BACKUP_DISK_CHECK_FAILED`: a snapshot written into a nearly full etcd
volume is worse than a snapshot that was skipped and reported, because the
write itself is what fills the volume the live cluster still needs.

### The restore point

A rollback request must name the snapshot it restores, by absolute path — an
operator restoring a cluster should not have to remember a file name. The
read-only backup view therefore answers `restore_point`: the newest
`pre_upgrade` snapshot, because that file describes the state immediately before
the last change, falling back to the newest recurring backup on a cluster that
was never upgraded. Each point carries `name`, `path`, `kind`, `version`,
`age_hours`, `listed`.

`listed` is what keeps the answer honest: `true` means a run read the file in
the backup directory, `false` means the platform only knows about it from a
`SUCCEEDED` upgrade command whose `etcd_snapshot` reported `verified: true`. A
pending or failed upgrade never contributes a restore point, because the
platform may not offer a file a command never proved it wrote.

### Backing up on demand

`POST /api/v1/cluster-deployments/<id>/etcd-backup/run/` queues one run now,
for an operator who is about to do something risky rather than waiting for the
hourly tick. It needs no approval and answers `202` with the command id and
`created`; pressing it again inside the same hour answers `202` with the
existing command and `created: false` (`ETCD_BACKUP_ALREADY_QUEUED`), because a
second snapshot of unchanged data is not a second backup. A cluster whose
`kubernetes.etcd_backup` is false, or that has no Target Agent, is refused with
`409` and `ETCD_BACKUP_DISABLED` / `ETCD_BACKUP_NO_TARGET_AGENT`.

### Checking the disk without writing to it

`POST /api/v1/cluster-deployments/<id>/etcd-backup/check/` queues the read-only
`cluster.kubernetes.backup.status` command: it lists the directory and reports
every entry, so an operator can see the truth between two backup runs. It needs
no approval and is deduplicated on a ten-minute bucket
(`ETCD_BACKUP_CHECK_ALREADY_QUEUED`). The read-only view consumes those results
too — `reported_by` / `reported_at` say which command produced the listing it is
describing — but a read-only check never replaces the `ETCD_BACKUP_NOT_RUN_YET`
finding: freshness is claimed by runs, not by someone looking.

### One directory for every snapshot of a cluster

A pre-upgrade snapshot exists so a failed upgrade can be undone, and it is the
same kind of file the recurring backup prunes. If the upgrade wrote to a
different directory, retention would never see it and a rollback would not
find it. The upgrade request therefore accepts an optional
`snapshot_directory`: the platform fills it from the deployment's
`kubernetes.etcd_backup_directory`, the Target Agent falls back to
`KUBERNETES_UPGRADE_ETCD_SNAPSHOT_DIR` and then to the backup contract default.
The directory must be under `/var/lib/etcd`, the only host path the etcd pod
mounts (`SNAPSHOT_DIRECTORY_INVALID` otherwise), because a snapshot the pod
cannot read is not a backup.

Before writing it, the upgrade measures the live database
(`etcdctl endpoint status`) against the free space of that volume (`df -PB1`).
When the database plus a 512 MiB floor (overridable with
`KUBERNETES_UPGRADE_ETCD_SNAPSHOT_MIN_FREE_BYTES`) does not fit, the upgrade is
refused with `ETCD_BACKUP_DISK_LOW` before anything on the node changes; an
unreadable measurement refuses with `ETCD_BACKUP_DISK_CHECK_FAILED`. Filling the
etcd volume during an upgrade is how a rollback becomes impossible.

### A change is followed by a backup

Once a command that changes a cluster succeeds, every snapshot on the volume
describes a state that no longer exists. The runtime result endpoint therefore
queues a backup in the same transaction that records the result, using the
deployment the command belongs to. It only does so when the command's own result
proves the change happened: an upgrade needs `upgrade_performed: true`, a
rollback needs `rolled_back: true`. A simulation succeeds without touching the
cluster and queues nothing. The hourly idempotency key still applies, so a change
inside an hour the scheduler already covered does not take a second snapshot.

### Is the scheduler still alive?

A scheduler that dies looks exactly like a scheduler between ticks until the
backups are old enough to look stale, so `queue_etcd_backups` writes one
`cluster.etcd_backup.scheduler.run` event per tenant per hour bucket. The
read-only view reports `scheduler_last_run` and `scheduler_age_hours`, and
`check_etcd_backups` upgrades a stale or never-run cluster to
`ETCD_BACKUP_SCHEDULER_SILENT` when no heartbeat arrived for
`SCHEDULER_SILENCE_HOURS` (2): old backups plus nothing queuing new ones is a
different incident, with a different fix, than a cluster that simply has old
backups.

### When the newest attempt fails

A failed run reports no listing, so the newest description of the directory
still comes from an earlier, successful run and the cluster would look healthy
while every hourly attempt has been failing. The state therefore also carries
`last_run_failed` — the failure code of the newest backup command — appends a
`backup.last_run` finding and clears `ok`. `check_etcd_backups` treats it as a
problem code (`ETCD_BACKUP_LAST_RUN_FAILED` when the run produced no code of its
own), so a disk that fills up is reported by the same watchdog that reports a
stale file.

### Reaching a human

An audit event and a non-zero exit are enough for CI but wake nobody. When
`ETCD_BACKUP_ALERT_WEBHOOK` is set, every `check_etcd_backups` run that found at
least one problem POSTs one JSON body (`kind: etcd_backup_problem`, the readable
`text`, and the problems with their codes, newest snapshot and scheduler age) to
that URL, with `ETCD_BACKUP_ALERT_TIMEOUT_SECONDS` (default 10) as the timeout.
The report's `alert` field says `configured` / `delivered` / `reason`
(`no_webhook`, `nothing_to_report`, `send_failed`, `http_status`) and never
echoes the URL, which may carry a token. A healthy fleet is never paged; dry
runs and `--no-alert` never page. Compose runs the probe hourly as
`etcd-backup-watchdog`, next to the scheduler that takes the backups.

### When the queue itself is the failure

A command that was queued and never finished looks like yesterday's healthy
snapshot from the outside. When a backup command is still `queued`/`running`
after `STUCK_AFTER_HOURS` (6), the state reports `run_stuck_hours` and a
`backup.queue` finding with `ETCD_BACKUP_RUN_STUCK`, and the watchdog reports it:
the agent never claimed the lease, so waiting longer only delays the day the
gap is noticed. The question is asked of the table, not of the twelve-entry
history window, so a command dropped long ago is still found. A run inside the
window is left alone.

### Staging files are not allowed to pile up

A save that dies mid-write, or a run whose process is killed outright, leaves a
`*.db.partial` behind. Retention only ever looks at naming-valid snapshots, so
nothing would delete it and it can be as large as the whole database. The
executor therefore removes the staging file on a failed save or failed verify,
keeps a verified file that failed only to be renamed (`staging_kept` names it),
and, as part of `prepare`, sweeps staging files older than
`PARTIAL_STALE_MINUTES` (24h) that sit directly in the backup directory —
nothing outside it and nothing that is not a staging file is ever touched. The
run reports what it swept in `swept_partials`.

The pre-upgrade snapshot follows the same discipline: it is saved as
`snapshot-pre-<cluster>-<target>-<stamp>.db.partial`, read back with
`snapshot status`, and only then renamed with `sudo mv`. A file that fails to
save or to read back is deleted, so a corrupt snapshot can never carry a real
snapshot name and be offered as a restore point by the next listing; a rename
that fails leaves the verified `.partial` in place and blocks the upgrade with
its path in the detail.

### A dead command does not block the hour

The hourly idempotency key stops two snapshots of the same data, but it must not
stop a retry: when the command for this bucket already ended `failed` or
`cancelled`, `POST .../etcd-backup/run/` (and the scheduler) queue a new command
under `<key>:retry:<n>` instead of handing back the dead one, so an operator in
the middle of an incident does not have to wait for the next hour. A live retry
already covering the bucket is returned rather than duplicated, so double
clicks stay idempotent. Read-only checks follow the same rule in their
ten-minute bucket, so a failed probe can be repeated immediately.

### Overrides and their defaults

`queue_etcd_backups --directory/--keep-count/--keep-max-age-hours/
--pre-upgrade-keep-max-age-hours/--min-free-bytes` default to "unset" rather
than to 0, so a scheduler that passes no flag uses each deployment's own policy
instead of silently resetting it; `--min-free-bytes 0` is still forwarded
verbatim, because zero there means "measure the volume without refusing the
backup".

### Switched off on purpose

A deployment whose spec sets `kubernetes.etcd_backup: false` reports
`enabled: false` with no findings, instead of painting "never backed up" over an
intentional choice; the watchdog keeps reporting `ETCD_BACKUP_DISABLED` for it
and does not audit it as a problem. The Delivery table says so in words.

`keep_count` counts recurring snapshots, not listing positions: pre-upgrade
files above them in the newest-first listing must not consume those slots and
cause a periodic backup inside the count to be deleted.

The watchdog entry now also carries `last_run_failed` and `run_stuck_hours`, so
a log line or a dashboard can tell a cluster with old backups (nothing queued),
a cluster whose newest attempt failed, and a cluster with a command nobody ran,
without re-reading the whole report.

A cluster that was upgraded before its first recurring backup still has a
restore point: `assess` used to return `restore_point: null` whenever no periodic
snapshot existed, so the panel said no snapshot could be restored while the
pre-upgrade file sat on disk. The restore-point finding is now reported in that
branch too, and the missing recurring backup stays a finding of its own.

A restore point the platform only learned from an upgrade report carries
`listed: false`, and the Delivery table says so next to it: the file was verified
when it was written, but no later directory listing has seen it, so an operator
knows to check the path before using it.

The on/off switch is read strictly: `spec.kubernetes.etcd_backup` must be a JSON
boolean. A hand-edited `"false"`, `0` or `"no"` is reported as
`ETCD_BACKUP_ENABLED_INVALID` by the view and the watchdog, and the scheduler
skips the cluster under that code instead of guessing — `bool("false")` is
`True`, and either guess switches backups the wrong way half the time.
