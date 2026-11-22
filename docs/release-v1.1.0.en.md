# AI Agent Stack 1.1.0 — Release Notes (English)

> 中文全文见 `docs/release-v1.1.0.zh-CN.md`.

1.1.0 is the first feature release after 1.0.0, and its subject is **unattended etcd
backups**. In 1.0.0 the only snapshot was a side effect of an upgrade or a rollback:
once a cluster was installed, nothing protected it periodically. This release
turns that into a product capability — contract, executor, retention policy,
scheduler, read-only platform view and a UI panel, with nothing missing in between.

- Version: `1.1.0`, still a single source in `pyproject.toml`, read through
  `packages/version.py::project_version()`.
- Bilingual: this file is the English edition; the Chinese one is
  `docs/release-v1.1.0.zh-CN.md`.
- History: `CHANGELOG.md` (`[1.1.0]` entry).
- Positioning: a **code-level feature release**. The automated baseline is green;
  the physical etcd acceptance items have not been run (see §7).

---

## 1. What this release fixes

| 1.0.0 | 1.1.0 |
| --- | --- |
| Only a pre-upgrade / pre-rollback snapshot in `/var/lib/etcd/backups`; no period, no retention, no reporting | Periodic queueing, verified publishing, count-and-age retention, read-only reporting |
| A failed save can leave a truncated file that looks like a backup | Write `.partial`, rename only after `snapshot status` passes |
| Snapshots pile up until the disk fills | Retention decided by the contract: never delete the newest, never delete anything younger than an hour, never delete a snapshot a rollback holds |
| The platform cannot tell whether a backup is fresh | Command result + `GET .../etcd-backup/` + a Backups column, showing the newest file and its age |

---

## 2. Contract: `packages/agent_contracts/etcd_backup.py`

- Command surface: `cluster.kubernetes.backup.run` (writes files, takes a lease,
  at-most-once, and deliberately **not** in the approval queue — a backup that
  waits for a person is not a backup) and `cluster.kubernetes.backup.status`
  (read-only).
- `validate_request`: `cluster_name` must be a DNS label; `execution_mode` is
  `simulation` or `execute` and `execute` must echo `confirm_cluster_name`;
  `directory` must sit under `/var/lib/etcd` with no `..`; `keep_count` is
  2–168 (default 2), `keep_max_age_hours` caps at 8760 (default 8760, i.e.
  count-only retention), `max_age_hours` defaults to 26; unknown keys are
  refused.
- Naming: `backup_filename` → `<cluster>-<yyyyMMddTHHMMSSZ>.db`,
  `partial_name` appends `.partial`, `parse_backup_name` reads it back;
  `snapshot_entries` keeps only files of this cluster that parse, so a stray
  file can never take part in a retention decision.
- Retention: `select_prunable` applies count and age, on top of
  `MIN_AGE_HOURS = 1` and the caller's `protect` set; deletion is decided here
  and nowhere else.
- Assessment: `assess` returns `count` / `newest` / `newest_age_hours` /
  `prunable` / `findings`, with `ETCD_BACKUP_MISSING` for no snapshot and
  `ETCD_BACKUP_STALE` for an old one; `simulation_plan` is four steps
  (save → verify → publish → retention).

## 3. Executor: `apps/target_agent/etcd_backup_exec.py`

- `_etcd_pod()` picks a Running `etcd-` Pod in `kube-system`; `_etcdctl_argv()`
  passes the three certificates under `/etc/kubernetes/pki/etcd`;
  `_list_directory()` parses `ls -ln` (busybox compatible) and treats a missing
  directory as empty.
- `run()` runs `prepare / snapshot_save / snapshot_verify / snapshot_publish /
  retention`. A failed verify removes the `.partial` file and **stops before
  retention**, so a broken backup can never cause a good one to be deleted.
  Failure codes: `ETCD_BACKUP_PREPARE_FAILED`, `_SAVE_FAILED`, `_VERIFY_FAILED`,
  `_PUBLISH_FAILED`, `_POD_NOT_FOUND`.
- `status()` reads only: no snapshot, no file change; it answers with entries,
  the newest age and the findings.

## 4. Scheduling and operations

- `python apps/platform-api/manage.py queue_etcd_backups` walks every `succeeded`
  `ClusterDeployment` and queues one command per hour bucket, idempotent per
  cluster (`etcd-backup:<deployment_id>:<YYYYMMDDTHH>`), using the deployment's
  `requested_by` as requester, and prints a JSON report
  `{bucket, queued, skipped, dry_run, at}`. `--dry-run` / `--directory` /
  `--keep-count` / `--keep-max-age-hours` override the policy;
  `spec.kubernetes.etcd_backup: false` or a deployment without a Target Agent
  is reported with a reason instead of being queued.
- `deploy/docker-compose.yml` and `deploy/docker-compose.cloud.yml` gain an
  `etcd-backup-scheduler` service that runs that command every
  `ETCD_BACKUP_INTERVAL_SECONDS` (default 3600), with `restart: unless-stopped`
  and `no-new-privileges`. An external cron calling the same command is also
  safe: the hour bucket makes duplicates harmless.
- `GET /api/v1/cluster-deployments/<id>/etcd-backup/` (read-only) answers with
  the declared policy (`kubernetes.etcd_backup`, `etcd_backup_keep_count`,
  `etcd_backup_keep_max_age_hours`, `etcd_backup_max_age_hours`,
  `etcd_backup_directory`, all validated by the contract), the newest reported
  snapshot and its age, staleness, the prunable set and the findings. An invalid
  policy such as `keep_count: 1` is reported at configuration time with the
  contract error code instead of at 3 a.m.
- The Target Cluster Delivery table gains a **Backups** column: a `fresh` /
  `stale` tag, the newest file name and its age, and any failing reason.

## 5. Upgrade and compatibility

- No platform migration: backup state is derived from existing
  `AgentCommand.result`, and the scheduler only writes `AgentCommand`.
- A Target Agent must run this version to recognise
  `cluster.kubernetes.backup.run` / `.status`; an older agent rejects it as an
  unknown command and the scheduler records an ordinary failure, leaving the
  delivery chain untouched.
- Snapshots left in `/var/lib/etcd/backups` by an upgrade or a rollback are
  recognised by `snapshot_entries` and take part in retention. If a rollback
  still refers to one of them, let the restore finish first (the contract
  supports a `protect` set).
- The default policy is the most conservative one: keep 2, count-only. Ask for
  more with `kubernetes.etcd_backup_keep_count`.

## 6. Automated baseline

- Tests: `pytest` green (1666 from the 1.0.0 baseline plus 33 new), `ruff` green,
  frontend `npm test` (36) and `npm run build` pass.
- CI: `ci.yml` green (4/4 Python shards + frontend + Compose smoke + kind/Metal3
  CRD integration).
- New coverage: request validation and directory bounds; count, age, minimum-age
  and foreign-file retention; the save→verify→publish→prune order; a failed save
  skips retention; a failed verify removes `.partial` and does not prune; no etcd
  Pod; read-only status; one queue entry per hour, idempotency and skip reasons;
  a compose guard; the platform view for fresh, stale, invalid policy and a
  stranger.

## 7. Still needs physical hardware (not claimed as verified here)

1. Real `etcdctl snapshot save` / `status` timing and volume usage on a
   three-control-plane cluster.
2. Restoring an evicted member from a `.db` produced by this version (the
   physical half of the rollback chain).
3. Behaviour when the scheduler overlaps a cluster upgrade (snapshot
   consistency during an upgrade).
4. The failure path and alert delivery when the backup volume fills up.
5. The remaining 1.0.0 §6 GA gate items are unchanged: real Redfish/firmware
   upgrade, real PXE boot, physical bond + VLAN70, passwordless sudo across
   control planes, Ceph/SR-IOV, and `release-manifest.json`.

## 8. Release artifacts

- Source: the GitHub archives (`tar.gz` / `zip`) of this tag.
- Offline image list: `deploy/offline/images.json` (no new images this release).
- Docs: `docs/agent-command-api.md` (new backup section and read-only view),
  `CHANGELOG.md`, this file and the Chinese edition.
- License: Apache-2.0.
