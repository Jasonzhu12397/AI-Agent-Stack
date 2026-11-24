# Changelog

All notable changes to AI Agent Stack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `stack_get_deployment`: read one deployment's status, plan hash, preflight
  (network readiness, Ironic warnings) and plan phases from a chat turn.
- `stack_create_command` now validates the command against the node's role
  (EPH, Target, local runner) instead of queueing work its agent cannot poll.
- `stack_plan_deployment`: a chat turn can file a bare-metal cluster deployment
  for an online EPH node, validated by the same spec validator and network
  readiness gate the deployment form uses, with the preflight stored on the
  record.
- Local execution: `Local computer` threads run real read-only probes on the
  enrolled `local_runner` node (`apps/local-agent`), with the command, lease
  and result recorded by the platform. Tasks: `local.system_info`,
  `local.disk_usage`.
- Connector catalog: `deploy/agent-plugins/catalog.json` plus
  `manage.py sync_plugin_catalog` registers 企业微信 / 地图 / 天气 connectors,
  and the composer installs them once their endpoint variable is set.
- Chat tool calls: the console now executes the skills and plugins selected in
  the composer (platform control-plane tools plus connector capabilities),
  persists every call on the assistant message and renders one status chip per
  call. Scheduled turns use the same tool loop.

### Added

- **Chat workspace** — the Control Agent console is now a mainstream chat
  surface: one left rail (new task / new conversation / workbench / scheduled
  tasks / plugins · skills · partners / knowledge / more, project and
  conversation lists, account) and one full-width conversation column with
  Markdown, code and an Agent execution state. Task details open in a drawer,
  never a permanent third column. `AgentChatThread` gained `execution_target`,
  `effort`, `goal` and `plan_first`; `effort=low` really caps output tokens.
  `AgentChatAttachment` adds thread-scoped file uploads (20 MB, 10 per turn).
  `ScheduledChatJob` adds hourly/daily/weekly turns executed by the worker,
  with pause/resume and run-now. Interactive and scheduled turns share one
  `core/chat_runtime.py`, so a scheduled turn behaves like a typed one. See
  `docs/chat-workspace.md`.
- **Central Container Image Registry** — a mandatory platform capability for the
  image supply chain. `RegistryImage` / `RegistryEvent` model the catalog; the
  API refuses mutable tags, gates readiness on a passed security scan, requires
  a signature before production, walks `dev → staging → production` without
  skipping, and returns a digest-pinned pull plan on
  `PLATFORM_REGISTRY_HOST` (default `registry.ai-agent-stack.internal`). Web
  Studio exposes it as `Image Registry`. See `docs/container-image-registry.md`.
- **Workflow canvas viewport engine** — the canvas is now a real viewport: a
  single `translate(x, y) scale(k)` transform over a fixed-size plane, pointer
  anchored zoom, drag panning, `fitView()` with dock insets, minimap slice
  derived from the transform, per-workflow persistence, fullscreen and a
  bottom floating dock holding node creation, validate and the definition
  drawer. The app shell is a fixed-height flex layout, so the canvas fills the
  worksheet instead of being measured and resized from JavaScript.

### Fixed

- The backup disk verdict ignored bytes already held by in-flight `*.db.partial`
  staging files, so a volume that fit the database alone could be refused only
  after a half-written snapshot filled it. The pre-write check now counts staging
  occupancy (partials plus pending-publish records registered for this cluster)
  alongside the database size and the `min_free_bytes` floor, reports
  `free_bytes` / `occupancy_bytes` / `required_bytes` / verdict in the `disk`
  block, and re-measures the free floor after the write; a post-write shortfall
  fails with `ETCD_BACKUP_DISK_POST_WRITE_LOW` before any pruning. A zero floor
  remains measure-only. Cleanup failures are no longer swallowed: a `rm` the pod
  refused is recorded in `cleanup_failures` with path and error, left on disk for
  the next sweep to retry, while successful removals still report only through
  `swept_partials`.
- The mutating backup command did not check its command lease while executing: a
  lease lost mid-run could still publish a snapshot and then prune old ones. The
  executor now receives the same `lease_lost_event` the other mutating actions
  use, checks it before saving, before publishing, and before pruning, and stops
  with `COMMAND_LEASE_LOST_DURING_EXECUTION` once it is lost. After the loss it
  neither deletes nor overwrites anything: an interrupted verified snapshot keeps
  its staging file and a `pending_publish` sidecar for the next republish, and an
  already-published snapshot stays put with retention skipped. The dispatch path
  forwards the event instead of dropping it.
- Two backup runs of the same cluster in the same second collided on the
  second-resolution stamp, and the publish rename could silently overwrite an
  existing final snapshot. The staging path now carries a per-run id; the final
  name keeps the legacy stamp name when it is free and otherwise takes a
  suffixed, non-colliding name, so an existing final is never overwritten. The
  suffixed name still parses back to its cluster and stamp, and republish of a
  pending snapshot keeps the recorded final name, so retries do not accumulate
  garbage.
- The reactive backup slot after a cluster change is now bound to the mutation
  command that triggered it instead of a coarse per-hour `:change` label. The
  slot identity distinguishes the recurring hourly slot from the change slot;
  the change key carries the source command id
  (`etcd-backup:<deployment>:<hour>:change:cmd:<command-id>`), so a repeated
  delivery of the same command reuses one slot while two distinct mutations in
  the same hour keep two slots. The failed-slot retry chain (`:retry:N`) still
  hangs off the same key. Legacy hourly rows keep their original key scheme and
  are still reused; legacy coarse `:change` rows are historical records only and
  are never reused, so they cannot become ghost duplicates.
- The "who must be kept" protect set is now built in exactly one place:
  `core.etcd_backup_policy.protected_entries` returns source-tagged snapshots
  (`rollback_active`, `rollback_outcome_unknown`, `upgrade_reported`), and the
  backup payload, panel assessment, executor prune, and executor status report
  all derive from it instead of recomputing the list separately. A failed
  rollback releases its snapshot only when the restore consumed it
  (`rolled_back` is true) or the failure code proves the snapshot was unusable;
  unknown-outcome and never-executed failures (manual reconciliation required,
  approval expired, approval hash mismatch, ownership fenced, role forbidden,
  lease expired) keep protecting, and unknown failure codes fail safe by
  keeping the file. A verified snapshot reported by a settled upgrade enters the
  protect set, and the panel marks it unconfirmed with a reason when it is not
  present in the recent listing or no listing exists yet, instead of pretending
  it was confirmed on disk. In the executor, prune and assessment consume one
  protect variable read once at run entry, rather than two independent reads of
  the request payload.
- An approved rollback whose Target Agent went offline before ever claiming the
  lease no longer protects its snapshot indefinitely. Protection is now a
  bounded lease: an approved, unclaimed rollback past the deployment's
  protection horizon (default 24 hours, overridable through
  `spec.kubernetes.etcd_backup_rollback_protection_minutes`) is swept once into
  the terminal `expired` state, writes one
  `cluster.kubernetes.rollback.protection.released` audit event, and releases
  the snapshot; running or claimed rollbacks keep protecting. The sweep is
  idempotent (`select_for_update`, status re-check under lock, one audit event
  per transition), and a backup command's `protect` set is recomputed fresh
  when the agent claims it, so a rollback created after the backup was enqueued
  is still covered instead of relying on the frozen enqueue-time copy.
- Snapshots left in a retired backup directory no longer leak or disappear
  from the panel. Retention and the state view derive the candidate location
  set from the cluster's own records (current spec directory, past backup/status
  command directories, upgrade snapshot parent directories, and policy-update
  audit events) instead of reading only the current spec single value. Each
  entry is tagged with its own directory and pruned at its absolute path; the
  report carries `pruned_entries` with name and directory, and an unconfirmed
  restore point is marked as such.
- Commands created through the node command endpoint no longer leave their
  deployment link empty: the deployment the Target Agent serves is resolved at
  creation and persisted on the command. Rollback snapshot protection, the
  reported restore point and the post-change backup trigger all read that link,
  so an open rollback actually holds the snapshot it named instead of relying
  on an empty filter that could never match. A one-off migration backfills legacy
  unattributed commands from the same node-to-deployment chain; read-time code
  does not guess a deployment as a fallback.
- The etcd backup scheduler heartbeat was recorded once per tenant, so one
  queued cluster lent its freshness to every other cluster in that tenant:
  agentless, policy-disabled and never-queued clusters looked evaluated, and the
  watchdog could not tell a stopped scheduler from a quiet hour. The heartbeat
  is now written per deployment, carrying the cluster's own evaluation outcome
  (`queued` with its command id, or `skipped` with its reason), and the state
  reader trusts only that deployment's own record; legacy tenant-level rows are
  ignored on read.
- Pre-upgrade snapshots (`snapshot-pre-<version>-<stamp>.db`) were invisible to
  the backup contract, so the recurring retention pass never pruned them and one
  file piled up per upgrade forever. They are now recognised (both the new
  `snapshot-pre-<cluster>-<version>-<stamp>.db` naming and the older file names
  that carry no cluster), kept by their own
  `pre_upgrade_keep_max_age_hours` window (default 168h), and the newest one is
  never deleted because that is what an undo of the last change restores.
- The upgrade executor writes the cluster name into the snapshot file name so a
  file in the backup directory can be attributed to the cluster that owns it.

- A rollback holds the snapshot it will restore: the scheduler passes that file
  name as `protect` so the retention pass can never delete it, and the read-only
  view shows the protected set. A rollback that failed or was cancelled stops
  protecting its file.
- A verified etcd snapshot whose publish rename failed used to be kept under
  its `.partial` name with no state record, so the 24h mtime sweep eventually
  deleted it and no run ever republished it. It now carries a sidecar state
  record written at verify time (`pending_publish` / `published` / `abandoned`,
  with cluster, final name, directory, verify time, attempt count and last
  failure); the sweep and the republish pass read that state instead of the
  mtime. Pending snapshots inside the republish window (default 24h, up to 3
  publish attempts) are kept and republished by the next run, idempotently and
  without overwriting a newer final file; past the window or after the attempt
  limit the record turns `abandoned` and only then the file is removed, listed
  in the report. Partials without a readable record still follow the age sweep.

### Added

- Alerting: `check_etcd_backups` POSTs its problems to
  `ETCD_BACKUP_ALERT_WEBHOOK` (one JSON body with a readable summary, no page for
  a healthy fleet, dry runs or `--no-alert`), reports whether delivery worked
  without echoing the target, and compose runs it hourly as
  `etcd-backup-watchdog`.
- A change-triggered backup queues under its own key suffix instead of reusing
  the hour's scheduled command, whose snapshot describes the state before the
  change.
- Queueing the snapshot after a change runs in a savepoint, so a collision
  cannot cost the platform the report of a change that really happened; the
  failure is audited as `cluster.etcd_backup.queue_failed`.
- The staging-file sweep only removes `*.db.partial` files it can attribute to
  this cluster, so a run still writing under another cluster name in a shared
  backup directory is not deleted halfway.
- A change that could not queue its snapshot for any reason other than the
  policy being off is audited as `cluster.etcd_backup.change_not_queued`, so a
  missing post-change backup is explainable later.
- The scheduler queues one backup per cluster (same tenant, project, Target
  Agent and name), newest succeeded deployment first, so a reinstalled cluster
  no longer takes two snapshots an hour or lets two retentions prune each other.
- The scheduler and the watchdog now share one definition of "which deployment
  speaks for this cluster" (`core/installed_clusters.py`), so a superseded
  reinstall record cannot be queued twice or watched as a dead cluster.
- A cluster whose only snapshots are pre-upgrade ones now reports the files its
  retention policy would delete, instead of reporting nothing to prune because
  no recurring backup exists yet.
- The newest backup run is read from its own query, so a dozen "check now"
  commands can no longer push a successful run out of the shared history
  window and make the panel say the cluster was never backed up.
- `enabled` must be a boolean: a policy request carrying `"false"` is refused with
  `ETCD_BACKUP_ENABLED_INVALID` instead of switching backups on through
  `bool("false")`, and a hand-edited spec value is reported and skipped rather
  than guessed.
- The verification matrix points at run 45090647438 (`ac42474d`, 1814 passed).
- A restore point reported only by an upgrade is labelled `listed: false` in the
  Delivery table, so its path is not mistaken for a file a listing has seen.
- The read-only check follows the same retry rule: a failed check no longer
  answers "already queued" for its ten-minute bucket.
- The watchdog entry also carries `enabled`, so a disabled cluster is visible in
  the dump without reading its state.
- The verification matrix points at run 45088400861 (`9fff2092`, 1812 passed).
- A pre-upgrade snapshot counts as a restore point even before the first
  recurring backup exists (`assess` no longer returns `restore_point: null`
  when only pre-upgrade files are present).
- The watchdog report entry carries `last_run_failed` and `run_stuck_hours`
  alongside the existing codes.
- `keep_count` counts recurring snapshots only: recent pre-upgrade files no
  longer use up those slots and push a periodic backup out of retention.
- A rollback that already succeeded releases the snapshot it restored from,
  so retention is no longer told to hold that file forever.
- A cluster with backups switched off reports `enabled: false` and no findings
  rather than looking like a cluster that never backed up.
- Scheduler overrides default to unset instead of 0 (an unset flag no longer
  overwrites the deployment's policy), and an explicit `--min-free-bytes 0`
  reaches the command as a real value.
- A failed backup command no longer blocks the hour: a retry is queued under
  `<key>:retry:<n>`, while a live retry or a successful command is still
  returned instead of duplicated.
- The pre-upgrade snapshot is staged: written as `*.db.partial`, verified there,
  and renamed only afterwards; a failed save or verify deletes it so an
  unreadable file can never be offered as a restore point.
- Abandoned staging files are removed: a failed save or verify deletes its
  `.partial`, a verified file that failed to be renamed is reported as
  `staging_kept`, and `prepare` sweeps staging files older than 24h inside the
  backup directory (`swept_partials`). Failure reports now also carry what was
  measured (`verdict`) instead of dropping it.
- A queued backup no agent ever ran is reported: after six hours the state
  reports `run_stuck_hours` and `ETCD_BACKUP_RUN_STUCK` — found by querying
  unfinished commands, not by scanning the twelve-entry history — and the
  watchdog includes it in its problems.
- A failed newest backup attempt is visible: the state reports
  `last_run_failed` and a `backup.last_run` finding instead of judging the
  cluster healthy on an older snapshot, and the watchdog reports it.
- A scheduler heartbeat: `queue_etcd_backups` writes one
  `cluster.etcd_backup.scheduler.run` event per tenant per hour bucket, the view
  reports `scheduler_last_run` / `scheduler_age_hours`, the Delivery table shows
  it, and `check_etcd_backups` reports `ETCD_BACKUP_SCHEDULER_SILENT` (rather
  than plain staleness) when old backups are combined with no heartbeat for two
  hours.
- A backup is queued automatically after a command that really changed the
  cluster (a successful upgrade with `upgrade_performed: true`, a rollback with
  `rolled_back: true`), in the same transaction that records the result. A
  simulation queues nothing, and the hourly idempotency key still applies.

### Fixed

- The pre-upgrade snapshot and the recurring backup could write to two different
  directories, so retention could not see the upgrade's file and a rollback
  could not find it. The upgrade request now carries `snapshot_directory`
  (filled by the platform from `kubernetes.etcd_backup_directory`, validated to
  be under `/var/lib/etcd`), and the executor measures the database against the
  volume's free space before writing: a snapshot that does not fit is refused
  with `ETCD_BACKUP_DISK_LOW` before the node changes, and an unreadable
  measurement refuses with `ETCD_BACKUP_DISK_CHECK_FAILED`.

### Added

- A disk check between `prepare` and the save: the run reads `dbSize` from
  `endpoint status` and free space from `df`, refuses to write when the database
  plus `min_free_bytes` (default 512MiB, `ETCD_BACKUP_DISK_LOW`) does not fit,
  and fails closed with `ETCD_BACKUP_DISK_CHECK_FAILED` when either measurement
  cannot be read. The floor is policy (`kubernetes.etcd_backup_min_free_bytes`)
  and is editable from the policy dialog in GiB.
- `POST .../etcd-backup/check/` and a Check now button: queue the read-only
  `cluster.kubernetes.backup.status` command (ten-minute bucket) so an operator
  can learn what is actually on the disk between two runs. The read-only view
  now consumes status listings as well as run listings and reports
  `reported_by` / `reported_at`, while keeping `ETCD_BACKUP_NOT_RUN_YET` when no
  scheduled run has ever reported.
- A `restore_point` on the read-only backup view: the newest pre-upgrade snapshot
  (or, on a cluster never upgraded, the newest recurring backup) with its path,
  kind, version, age and a `listed` flag that separates "a run saw this file"
  from "only a succeeded upgrade reported it". The rollback form can fill its
  snapshot path from it instead of asking an operator to recall a file name.
- `POST .../etcd-backup/run/` and a "Back up now" button: an operator can queue
  a run without waiting for the hourly tick. The scheduler and the button share
  one queueing module and one idempotency key, so an hour the scheduler already
  covered returns that command (`ETCD_BACKUP_ALREADY_QUEUED`) instead of taking
  a second snapshot. `ETCD_BACKUP_DISABLED` / `ETCD_BACKUP_NO_TARGET_AGENT`
  refuse it with 409.
- `check_etcd_backups`: reports every installed cluster with a missing, stale or
  never-run backup, writes a `cluster.etcd_backup.problem` audit event for each
  and exits non-zero, so a cron entry or monitoring probe can alarm without a
  human watching the table. `--dry-run` reports without auditing, and a cluster
  that opted out with `kubernetes.etcd_backup: false` is reported as disabled
  rather than as a problem.

- `POST /api/v1/cluster-deployments/<id>/etcd-backup/policy/` and a policy dialog
  in the delivery table: the contract validates the change before it is written
  into `spec.kubernetes`, every accepted change is audited, and unknown keys are
  refused instead of dropped.

### Changed

- `queue_etcd_backups` honours the retention policy a deployment declares in
  `spec.kubernetes` (`etcd_backup_keep_count`, `etcd_backup_keep_max_age_hours`,
  `etcd_backup_pre_upgrade_keep_max_age_hours`, `etcd_backup_max_age_hours`,
  `etcd_backup_directory`); command-line flags remain the override. A policy the
  contract refuses is reported as a skip reason instead of being queued.

## [1.1.0] — 2026-11-22

### Added

- Unattended etcd backups: `cluster.kubernetes.backup.run` writes
  `etcdctl snapshot save` into `/var/lib/etcd/backups/<cluster>-<UTC>.db` through a
  `.partial` file that only becomes visible after `snapshot status` verifies it,
  then prunes by count and by age while never deleting the newest backup, never
  deleting anything younger than one hour and never deleting a snapshot a
  rollback is holding. `cluster.kubernetes.backup.status` reports the newest
  backup without touching anything.
- `queue_etcd_backups` management command and an `etcd-backup-scheduler` compose
  service: every succeeded Target cluster gets one backup command per hour bucket,
  idempotent per cluster, skipping a cluster whose spec sets
  `kubernetes.etcd_backup: false` or that has no Target Agent.
- `GET /api/v1/cluster-deployments/<id>/etcd-backup/` and a Backups column on
  the Target Cluster Delivery table: declared retention policy, newest snapshot
  and its age, staleness, prunable files and contract findings, all read from
  what commands already reported.
- `docs/release-v1.1.0.zh-CN.md` and `docs/release-v1.1.0.en.md`: bilingual notes
  for this release.

## [1.0.0] — 2026-11-22

The first release that is deployable as a product: the SaaS Agent platform and
the three-Agent bare-metal delivery chain in one versioned artifact, with one
automated baseline, one offline image list and one release gate.

### Added

- Single source for the product version (`packages/version.py`), read from
  `pyproject.toml`, exposed on `/api/v1/`, stamped into the release manifest and
  overridable with `AI_AGENT_STACK_VERSION` for stripped images.
- `LICENSE` (Apache-2.0).
- `docs/release-v1.0.0.zh-CN.md` and `docs/release-v1.0.0.en.md`: feature
  inventory, deploy paths, support matrix, GA gate list and the items that still
  require a physical lab, in Chinese and English.
- `make verify` (ruff + pytest + frontend tests + frontend build) and
  `make version`.
- `tests/test_dockerfile_packages.py`: every Python image must copy the whole
  `packages/` tree, so a new shared module can never break an image again.
- Release manifest now records `product_version` beside `commit` and evidence
  hashes, so two releases cannot be confused by an identical evidence directory.

### Changed

- Every Python image copies `packages/` as a tree instead of a hand-listed
  subset of subdirectories; `.dockerignore` excludes caches and build output.
- `tests/test_health.py` asserts the version from `project_version()` instead of
  a literal, and `metal3_release_evidence` takes the same value.
- Version bumped to `1.0.0` in `pyproject.toml`, `apps/web-studio/package.json`
  and the lockfile.

### Fixed (root causes, from the v0.3.0 cycle)

- **Rollback can no longer leave a cluster down**: one `_fail` path restores
  swapped data directories, starts every stopped kubelet with the local member
  first, probes the apiserver, and returns `recovery` plus a three-branch
  `next_step`.
- **Upgrade completion compares the full version triple**: `v1.34.0` is no
  longer treated as satisfying `v1.34.3`.
- **VLAN topology renders correctly**: a profile may declare a segment by its
  sub-interface (`bond_control.70`), the contract stores the base and derives
  `<base>.<vlan>`; a role that joins only a VLAN gets the bond and its members
  rendered, and `pxe_boot_ports` walks one VLAN hop back to physical members.
- **A data bond's members can come from PXE-collected PCI addresses**
  (`pci@<addr>`), so the UI never retypes kernel names; an empty bond is still
  refused.
- **Snapshot visibility, remote sudo and IPv6 peers**: snapshot must live under
  `/var/lib/etcd`, `ssh.sudo/<node>` is a preflight check, and bracketed IPv6
  peer URLs match their members.
- **Acceptance evidence names what would change**: `local_node`, `members` and
  the five rollback stages, so an approver reviews mutations, not a summary.

## [0.3.0] — 2026-11-22

### Added

- Version read from one place, stamped into the release manifest.
- Release notes for the bare-metal delivery baseline.
- `make verify` and `make version`.

### Fixed

- Images that copied a hand-listed subset of `packages/` failed to boot with
  `ModuleNotFoundError` when a new shared module appeared.

## [0.2.0] — earlier

Django/Vue migration baseline: multi-tenant projects and roles, Agent and
Workflow APIs, durable PostgreSQL worker, Metal3 cluster-plan validation,
EPH pre-flight orchestration, control workspace and the three infrastructure
Agents. Test baseline at that time: 103 passed.

## Earlier history

Before `0.2.0` the repository ran a FastAPI + React stack; that history stays in
the git log and in `docs/verification-matrix.md`, and is not re-versioned here.
