# Changelog

All notable changes to AI Agent Stack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

### Added

- Alerting: `check_etcd_backups` POSTs its problems to
  `ETCD_BACKUP_ALERT_WEBHOOK` (one JSON body with a readable summary, no page for
  a healthy fleet, dry runs or `--no-alert`), reports whether delivery worked
  without echoing the target, and compose runs it hourly as
  `etcd-backup-watchdog`.
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
