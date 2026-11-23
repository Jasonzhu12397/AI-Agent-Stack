"""Recurring etcd backups: the retention rules, and the order of a run."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from agent_contracts.etcd_backup import (
    DEFAULT_DIRECTORY,
    EtcdBackupError,
    assess,
    backup_filename,
    parse_backup_name,
    partial_name,
    restore_point,
    select_prunable,
    simulation_plan,
    snapshot_entries,
    validate_request,
)
from target_agent.etcd_backup_exec import EtcdBackupExecutionError, EtcdBackupExecutor

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)


def _name(hours_ago: int, cluster: str = "lab") -> str:
    stamp = (NOW - timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")
    return f"{cluster}-{stamp}.db"


def _entries(*ages: int, cluster: str = "lab") -> list[dict]:
    return snapshot_entries(
        [{"name": _name(age, cluster), "size_bytes": 1} for age in ages],
        cluster_name=cluster,
    )


# ---------------------------------------------------------------- validation


def test_defaults_come_from_the_directory_the_etcd_pod_mounts():
    request = validate_request({"cluster_name": "lab"})
    assert request["directory"] == DEFAULT_DIRECTORY
    assert request["keep_count"] == 2
    assert request["execution_mode"] == "simulation"


@pytest.mark.parametrize(
    "directory",
    ["/var/backups/etcd", "/var/lib/etcd/../secrets", "/tmp/etcd", "etcd"],
)
def test_a_directory_the_etcd_pod_cannot_see_is_refused(directory):
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "directory": directory})
    assert error.value.code == "ETCD_BACKUP_DIRECTORY_INVALID"


def test_a_retention_that_would_keep_one_file_is_refused():
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "keep_count": 1})
    assert error.value.code == "ETCD_BACKUP_RETENTION_INVALID"


def test_execute_needs_the_cluster_name_echoed_back():
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "execution_mode": "execute"})
    assert error.value.code == "ETCD_BACKUP_CONFIRMATION_MISMATCH"
    request = validate_request(
        {
            "cluster_name": "lab",
            "execution_mode": "execute",
            "confirm_cluster_name": "lab",
        }
    )
    assert request["execution_mode"] == "execute"


def test_an_unknown_key_is_refused_instead_of_ignored():
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "retention": "forever"})
    assert error.value.code == "ETCD_BACKUP_PAYLOAD_INVALID"


# ---------------------------------------------------------------- retention


def test_the_newest_files_survive_even_when_they_are_old():
    entries = _entries(200, 210, 220, 230, 240)
    prunable = select_prunable(
        entries, keep_count=2, keep_max_age_hours=100, now=NOW
    )
    assert [item["name"] for item in prunable] == [_name(240), _name(230), _name(220)]


def test_a_file_younger_than_the_minimum_age_is_never_pruned():
    # Five files, keep two, and the third youngest is only 30 minutes old:
    # deleting it would remove a snapshot taken by this very job window.
    entries = snapshot_entries(
        [
            {"name": _name(0), "size_bytes": 1},
            {"name": _name(0.5), "size_bytes": 1},
        ],
        cluster_name="lab",
    )
    assert select_prunable(entries, keep_count=1, keep_max_age_hours=1, now=NOW) == []


def test_age_is_the_document_retention_not_just_the_count():
    entries = _entries(1, 2, 3, 4, 5)
    prunable = select_prunable(
        entries, keep_count=2, keep_max_age_hours=10, now=NOW
    )
    assert prunable == []


def test_a_foreign_or_malformed_file_never_enters_the_decision():
    entries = snapshot_entries(
        [
            {"name": _name(30), "size_bytes": 1},
            {"name": _name(30, cluster="other"), "size_bytes": 1},
            {"name": "etcd.db", "size_bytes": 1},
            {"name": _name(30) + ".partial", "size_bytes": 1},
            {"name": "lab-20261301T000000Z.db", "size_bytes": 1},
        ],
        cluster_name="lab",
    )
    assert [item["name"] for item in entries] == [_name(30)]


# ---------------------------------------------------------------- staleness


def test_a_stale_newest_snapshot_is_reported_and_fails_the_verdict():
    report = assess(
        _entries(40, 60, 80),
        keep_count=2,
        keep_max_age_hours=1000,
        max_age_hours=26,
        now=NOW,
    )
    assert report["ok"] is False
    findings = {item["name"]: item for item in report["findings"]}
    assert findings["backup.newest"]["code"] == "ETCD_BACKUP_STALE"


def test_no_snapshot_at_all_is_a_named_finding():
    report = assess(
        [], keep_count=2, keep_max_age_hours=100, max_age_hours=26, now=NOW
    )
    assert report["ok"] is False
    assert report["findings"][0]["code"] == "ETCD_BACKUP_MISSING"


def test_a_fresh_snapshot_passes():
    report = assess(
        _entries(1, 2),
        keep_count=2,
        keep_max_age_hours=100,
        max_age_hours=26,
        now=NOW,
    )
    assert report["ok"] is True
    assert report["count"] == 2


# ---------------------------------------------------------------- names and plan


def test_the_file_name_is_a_utc_stamp_that_parses_back():
    name = backup_filename("lab", now=NOW)
    assert name == "lab-20261122T030000Z.db"
    parsed = parse_backup_name(name, cluster_name="lab")
    assert parsed["taken_at"] == NOW.isoformat()
    assert partial_name(name) == name + ".partial"


def test_the_plan_says_rename_happens_only_after_verification():
    plan = simulation_plan(validate_request({"cluster_name": "lab"}), now=NOW)
    names = [step["name"] for step in plan["steps"]]
    assert names == [
        "prepare",
        "disk_check",
        "snapshot_save",
        "snapshot_verify",
        "snapshot_publish",
        "retention",
    ]
    publish = next(step for step in plan["steps"] if step["name"] == "snapshot_publish")
    assert "mv" in publish["commands"][0]
    save = next(step for step in plan["steps"] if step["name"] == "snapshot_save")
    assert ".partial" in save["commands"][0]
    # The disk check is a step of its own, before anything is written.
    assert names.index("disk_check") < names.index("snapshot_save")
    assert plan["approval_required"] is False


# ---------------------------------------------------------------- executor


class FakeRunner:
    """Answers the etcd pod calls a backup run makes."""

    def __init__(
        self,
        *,
        fail_on: str = "",
        files: list[str] | None = None,
        db_size: int = 8 * 1024 * 1024,
        free_bytes: int = 4 * 1024 * 1024 * 1024,
    ) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.files = list(files or [])
        self.db_size = db_size
        self.free_bytes = free_bytes
        self.find_output = ""

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self.fail_on and self.fail_on in joined:
            return {"available": True, "returncode": 1, "stdout": "", "stderr": "boom"}
        if "get" in argv and "pods" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps(
                    {"items": [{"metadata": {"name": "etcd-cp-0"}, "status": {"phase": "Running"}}]}
                ),
                "stderr": "",
            }
        if "find" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": self.find_output,
                "stderr": "",
            }
        if "endpoint" in argv and "status" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps([{"Header": {"dbSize": self.db_size}}]),
                "stderr": "",
            }
        if "df" in argv:
            return {
                "available": True,
                "returncode": 0,
                "stdout": (
                    "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
                    f"/dev/sda1 {self.free_bytes + 10} 10 {self.free_bytes} 1% /var\n"
                ),
                "stderr": "",
            }
        if "ls" in argv:
            lines = ["total 0"]
            for index, name in enumerate(self.files):
                lines.append(
                    f"-rw-r--r--    1 root     root       {1000 + index} Jan  1 00:00 {name}"
                )
            return {"available": True, "returncode": 0, "stdout": "\n".join(lines), "stderr": ""}
        return {"available": True, "returncode": 0, "stdout": "", "stderr": ""}


def _execute_request(**overrides):
    payload = {
        "cluster_name": "lab",
        "execution_mode": "execute",
        "confirm_cluster_name": "lab",
    }
    payload.update(overrides)
    return validate_request(payload)


def test_a_run_saves_verifies_renames_and_only_then_prunes():
    request = _execute_request(keep_max_age_hours=5)
    oldest = _name(6)
    runner = FakeRunner(files=[_name(4), _name(5), oldest])
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    steps = [step["name"] for step in result["steps"]]
    assert steps == [
        "prepare",
        "disk_check",
        "snapshot_save",
        "snapshot_verify",
        "snapshot_publish",
        "retention",
    ]
    assert result["backup"]["verified"] is True
    assert result["backup"]["name"] == "lab-20261122T030000Z.db"
    path_parts = set(result["backup"]["path"].split("/"))
    assert path_parts == {"", "var", "lib", "etcd", "backups", "lab-20261122T030000Z.db"}
    # Two are kept, and the 6h-old file is past the 5h document retention.
    assert result["pruned"] == [oldest]
    rm_calls = [call for call in runner.calls if "rm" in call]
    assert len(rm_calls) == 1
    assert any(oldest in argument for argument in rm_calls[0])


def test_a_failed_save_never_reaches_retention():
    request = _execute_request()
    existing = [_name(4), _name(5), _name(6)]
    runner = FakeRunner(fail_on="snapshot save", files=existing)
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_SAVE_FAILED"
    assert result["directory"] == DEFAULT_DIRECTORY
    assert result["pruned"] == []
    assert [step["name"] for step in result["steps"]] == [
        "prepare",
        "disk_check",
        "snapshot_save",
    ]
    # A save can die mid-write: the staging file is removed rather than left
    # to sit on the volume forever, because retention only looks at snapshots.
    removed = [call for call in runner.calls if "rm" in call]
    assert removed, "the abandoned staging file must be removed"
    assert ".partial" in removed[0][-1]


def test_a_failed_verification_removes_the_partial_and_prunes_nothing():
    request = _execute_request()
    runner = FakeRunner(fail_on="snapshot status", files=[_name(6)])
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_VERIFY_FAILED"
    assert result["pruned"] == []
    removed = [call for call in runner.calls if "rm" in call]
    assert removed, "the unverified partial must be removed"
    assert ".partial" in removed[0][-1]


def test_a_cluster_without_an_etcd_pod_is_refused_by_name():
    class NoPodRunner(FakeRunner):
        def __call__(self, argv, timeout=None):
            if "get" in argv and "pods" in argv:
                return {
                    "available": True,
                    "returncode": 0,
                    "stdout": json.dumps({"items": []}),
                    "stderr": "",
                }
            return super().__call__(argv, timeout=timeout)

    with pytest.raises(EtcdBackupExecutionError) as error:
        EtcdBackupExecutor(NoPodRunner()).run(_execute_request(), now=NOW)
    assert error.value.code == "ETCD_BACKUP_POD_NOT_FOUND"


def test_status_is_read_only_and_reports_the_age():
    runner = FakeRunner(files=[_name(2), _name(4)])
    report = EtcdBackupExecutor(runner).status(
        validate_request({"cluster_name": "lab"}), now=NOW
    )
    assert report["ok"] is True
    assert report["newest"]["name"] == _name(2)
    assert report["newest_age_hours"] == pytest.approx(2.0)
    assert not [call for call in runner.calls if "save" in call or "rm" in call]


# ------------------------------------------------- pre-upgrade snapshots


def _pre_upgrade(*ages: int, cluster: str = "lab", version: str = "v1.37.2") -> list[dict]:
    names = [
        f"snapshot-pre-{cluster}-{version}-"
        + (NOW - timedelta(hours=age)).strftime("%Y%m%dT%H%M%SZ")
        + ".db"
        for age in ages
    ]
    return snapshot_entries([{"name": name} for name in names], cluster_name=cluster)


def test_a_pre_upgrade_snapshot_is_recognised_and_attributed():
    entries = _pre_upgrade(10)
    assert entries[0]["kind"] == "pre_upgrade"
    assert entries[0]["cluster"] == "lab"
    assert entries[0]["version"] == "v1.37.2"


def test_an_older_pre_upgrade_name_without_a_cluster_is_still_this_nodes_file():
    name = "snapshot-pre-v1.37.2-20261122T030000Z.db"
    entries = snapshot_entries([{"name": name}], cluster_name="lab")
    assert [entry["name"] for entry in entries] == [name]
    assert entries[0]["kind"] == "pre_upgrade"
    assert entries[0]["cluster"] == ""
    assert entries[0]["version"] == "v1.37.2"


def test_a_recurring_backup_is_never_confused_with_a_pre_upgrade_snapshot():
    entries = snapshot_entries(
        [{"name": _name(3)}, {"name": "snapshot-pre-lab-v1.37.2-20261122T000000Z.db"}],
        cluster_name="lab",
    )
    assert [entry["kind"] for entry in entries] == ["periodic", "pre_upgrade"]


def test_the_newest_pre_upgrade_snapshot_survives_any_age():
    entries = _pre_upgrade(300, 400)
    prunable = select_prunable(
        entries,
        keep_count=2,
        keep_max_age_hours=5,
        pre_upgrade_keep_max_age_hours=24,
        now=NOW,
    )
    # The 300h file is the newest of its kind: a rollback of the last change
    # would restore it, so it is never pruned. The 400h one is past 24h.
    assert [item["name"] for item in prunable] == [entries[1]["name"]]


def test_pre_upgrade_snapshots_older_than_their_policy_are_pruned():
    entries = _pre_upgrade(30, 40, 50)
    prunable = select_prunable(
        entries,
        keep_count=2,
        keep_max_age_hours=5,
        pre_upgrade_keep_max_age_hours=24,
        now=NOW,
    )
    # Oldest first, and the 30h file is the newest of its kind so it stays.
    assert [item["name"] for item in prunable] == [entries[2]["name"], entries[1]["name"]]


def test_a_new_pre_upgrade_snapshot_is_never_pruned_and_protect_still_wins():
    entries = _pre_upgrade(2, 48, 72)
    prunable = select_prunable(
        entries,
        keep_count=2,
        keep_max_age_hours=5,
        pre_upgrade_keep_max_age_hours=24,
        now=NOW,
    )
    assert entries[0]["name"] not in [item["name"] for item in prunable]
    protected = select_prunable(
        entries,
        keep_count=2,
        keep_max_age_hours=5,
        pre_upgrade_keep_max_age_hours=24,
        now=NOW,
        protect=(entries[2]["name"],),
    )
    assert entries[2]["name"] not in [item["name"] for item in protected]


def test_a_pre_upgrade_snapshot_alone_is_not_a_fresh_backup_set():
    report = assess(
        _pre_upgrade(1),
        keep_count=2,
        keep_max_age_hours=8760,
        max_age_hours=26,
        now=NOW,
    )
    assert report["ok"] is False
    assert report["count"] == 0
    assert report["pre_upgrade_count"] == 1
    assert report["newest"] is None
    assert report["findings"][0]["code"] == "ETCD_BACKUP_MISSING"


def test_periodic_freshness_is_not_reported_from_a_pre_upgrade_snapshot():
    entries = snapshot_entries(
        [
            {"name": _name(50)},
            {"name": "snapshot-pre-lab-v1.37.2-20261122T020000Z.db"},
        ],
        cluster_name="lab",
    )
    report = assess(entries, keep_count=2, keep_max_age_hours=8760, max_age_hours=26, now=NOW)
    assert report["newest"]["name"] == _name(50)
    assert report["newest_age_hours"] == 50.0
    assert report["pre_upgrade_count"] == 1
    age = (NOW - datetime.fromisoformat(report["pre_upgrade"][0]["taken_at"])).total_seconds()
    assert age == 3600.0


def test_the_policy_carries_a_pre_upgrade_window():
    request = validate_request({"cluster_name": "lab"})
    assert request["pre_upgrade_keep_max_age_hours"] == 168
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "pre_upgrade_keep_max_age_hours": 0})
    assert error.value.code == "ETCD_BACKUP_RETENTION_INVALID"


# ------------------------------------------------------------- protect set


def test_protect_accepts_names_of_this_cluster_and_returns_them_sorted():
    request = validate_request(
        {
            "cluster_name": "lab",
            "protect": [
                "snapshot-pre-lab-v1.37.2-20261122T000000Z.db",
                "lab-20261122T010000Z.db",
                "lab-20261122T010000Z.db",
            ],
        }
    )
    assert request["protect"] == (
        "lab-20261122T010000Z.db",
        "snapshot-pre-lab-v1.37.2-20261122T000000Z.db",
    )


@pytest.mark.parametrize(
    "value",
    [
        "../lab-20261122T010000Z.db",
        "/var/lib/etcd/backups/lab-20261122T010000Z.db",
        "notes.txt",
        "other-20261122T010000Z.db",
    ],
)
def test_protect_refuses_anything_that_is_not_a_bare_snapshot_name(value):
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "protect": [value]})
    assert error.value.code == "ETCD_BACKUP_PROTECT_INVALID"


def test_a_run_never_prunes_a_snapshot_it_was_told_to_hold():
    held = _name(200)
    request = _execute_request(
        keep_count=2,
        keep_max_age_hours=5,
        pre_upgrade_keep_max_age_hours=5,
        protect=[held],
    )
    runner = FakeRunner(files=[_name(4), _name(5), held])

    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert held not in result["pruned"]
    assert held in [entry["name"] for entry in result["kept"]]
    assert not [
        call for call in runner.calls if "rm" in call and held in call
    ]


# --------------------------------------------------------------- disk check


def test_a_volume_too_small_for_the_database_plus_the_floor_is_refused():
    runner = FakeRunner(db_size=8 * 1024 * 1024 * 1024, free_bytes=1024 * 1024 * 1024)
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_DISK_LOW"
    # The measurements travel with the failure, so the report says how short.
    assert result["verdict"]["code"] == "ETCD_BACKUP_DISK_LOW"
    assert result["verdict"]["free_bytes"] == 1024 * 1024 * 1024
    assert result["backup"] is None
    assert [step["name"] for step in result["steps"]] == ["prepare", "disk_check"]
    assert not [call for call in runner.calls if "snapshot" in call and "save" in call]
    assert not [call for call in runner.calls if "rm" in call]


def test_enough_room_for_the_database_plus_the_floor_is_allowed():
    runner = FakeRunner(db_size=1024 * 1024 * 1024, free_bytes=2 * 1024 * 1024 * 1024)
    result = EtcdBackupExecutor(runner).run(
        _execute_request(min_free_bytes=512 * 1024 * 1024), now=NOW
    )

    assert result["failure_code"] is None
    assert result["backup"]["verified"] is True


def test_the_floor_is_policy_and_zero_means_measure_only():
    request = validate_request({"cluster_name": "lab", "min_free_bytes": 0})
    runner = FakeRunner(db_size=1024 * 1024, free_bytes=1024 * 1024)
    result = EtcdBackupExecutor(runner).run(request, now=NOW)
    assert result["failure_code"] is None


def test_a_database_size_that_cannot_be_read_blocks_the_run_instead_of_guessing():
    class NoStatusRunner(FakeRunner):
        def __call__(self, argv, timeout=None):
            if "endpoint" in argv and "status" in argv and "snapshot" not in argv:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "denied"}
            return super().__call__(argv, timeout=timeout)

    runner = NoStatusRunner()
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_DISK_CHECK_FAILED"
    assert not [call for call in runner.calls if "snapshot" in call and "save" in call]


def test_an_unreadable_free_space_blocks_the_run():
    class NoDfRunner(FakeRunner):
        def __call__(self, argv, timeout=None):
            if "df" in argv:
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "no df"}
            return super().__call__(argv, timeout=timeout)

    result = EtcdBackupExecutor(NoDfRunner()).run(_execute_request(), now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_DISK_CHECK_FAILED"


def test_the_contract_refuses_a_negative_disk_floor():
    with pytest.raises(EtcdBackupError) as error:
        validate_request({"cluster_name": "lab", "min_free_bytes": -1})
    assert error.value.code == "ETCD_BACKUP_DISK_POLICY_INVALID"


def _entry(name: str, cluster: str = "edge-lab") -> dict:
    return snapshot_entries([{"name": name}], cluster_name=cluster)[0]


def test_the_restore_point_prefers_the_snapshot_taken_before_the_last_change():
    pre = _entry("snapshot-pre-edge-lab-v1.34.1-20260101T000000Z.db")
    periodic = _entry("edge-lab-20260102T000000Z.db")

    point = restore_point([periodic, pre])

    assert point["name"] == pre["name"]
    assert point["kind"] == "pre_upgrade"
    assert point["version"] == "v1.34.1"
    assert point["age_hours"] > 0


def test_a_cluster_never_upgraded_restores_from_its_newest_recurring_backup():
    periodic = _entry("edge-lab-20260102T000000Z.db")

    point = restore_point([periodic])

    assert point["kind"] == "periodic"
    assert point["version"] is None


def test_without_any_snapshot_there_is_no_restore_point_and_the_assessment_says_so():
    report = assess(
        [], keep_count=2, keep_max_age_hours=8760, max_age_hours=26
    )

    assert restore_point([]) is None
    assert report["restore_point"] is None
    codes = {finding["code"] for finding in report["findings"]}
    assert "ETCD_BACKUP_MISSING" in codes


def test_a_healthy_assessment_names_the_file_a_rollback_would_use():
    entries = [_entry("edge-lab-20260102T000000Z.db")]

    report = assess(
        entries, keep_count=2, keep_max_age_hours=8760, max_age_hours=8760
    )

    assert report["restore_point"]["name"] == entries[0]["name"]
    finding = next(
        item for item in report["findings"] if item["name"] == "backup.restore_point"
    )
    assert finding["ok"] is True
    assert finding["code"] == ""


def test_a_failed_rename_keeps_the_verified_file_and_says_where_it_is():
    request = _execute_request()
    runner = FakeRunner(fail_on=" mv ", files=[])
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_PUBLISH_FAILED"
    # A verified snapshot under a staging name is still worth keeping, and the
    # report names it so an operator can publish it by hand.
    assert result["staging_kept"].endswith(".db.partial")
    assert ".partial" in result["staging_kept"]


def test_a_sweep_removes_abandoned_staging_files_and_nothing_else():
    request = _execute_request()
    keep = _name(2)
    staging = partial_name(_name(30, cluster=request["cluster_name"]))
    stale = f"{DEFAULT_DIRECTORY}/{staging}"
    shallow = f"{DEFAULT_DIRECTORY}/notes.db.partial"
    runner = FakeRunner(files=[keep, stale, shallow])
    runner.find_output = stale
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert result["swept_partials"] == [staging]
    removed = [call[-1] for call in runner.calls if "rm" in call]
    assert removed == [stale]
    assert keep not in removed


def test_a_sweep_leaves_another_clusters_staging_file_alone():
    request = _execute_request()
    staging = partial_name(_name(30, cluster="other"))
    runner = FakeRunner(files=[])
    runner.find_output = f"{DEFAULT_DIRECTORY}/{staging}"
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert result["swept_partials"] == []
    assert not [call for call in runner.calls if "rm" in call]


def test_a_sweep_never_deletes_a_path_outside_the_directory():
    runner = FakeRunner(files=[_name(2)])
    runner.find_output = f"{DEFAULT_DIRECTORY}/../etc/passwd.db.partial"
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert result["swept_partials"] == []
    assert not [call for call in runner.calls if "rm" in call]


def test_pre_upgrade_snapshots_do_not_use_up_the_recurring_keep_count():
    now = NOW
    periodic = [
        {"name": backup_filename("lab", now=now - timedelta(hours=2))},
        {"name": backup_filename("lab", now=now - timedelta(hours=3))},
        {"name": backup_filename("lab", now=now - timedelta(hours=4))},
    ]
    pre = [
        {"name": f"snapshot-pre-lab-v1.38.0-{stamp}.db"}
        for stamp in ("20261121T230000Z", "20261121T220000Z")
    ]
    entries = snapshot_entries(
        [*pre, *periodic], cluster_name="lab"
    )

    prunable = select_prunable(
        entries, keep_count=2, keep_max_age_hours=1, pre_upgrade_keep_max_age_hours=1000
    )

    # Two newest recurring snapshots stay even though two pre-upgrade files sit
    # above them in the listing; only the third periodic file may go.
    names = [entry["name"] for entry in prunable]
    assert periodic[0]["name"] not in names
    assert periodic[1]["name"] not in names


def test_a_pre_upgrade_snapshot_is_a_restore_point_before_the_first_backup():
    now = NOW
    name = "snapshot-pre-lab-v1.38.0-20261121T230000Z.db"
    report = assess(
        snapshot_entries([{"name": name}], cluster_name="lab"),
        keep_count=2,
        keep_max_age_hours=26,
        max_age_hours=26,
        now=now,
    )

    # No recurring backup has run yet, so the cluster is still not "ok"...
    assert report["ok"] is False
    assert report["findings"][0]["code"] == "ETCD_BACKUP_MISSING"
    # ...but the snapshot the upgrade took is real and can be rolled back to.
    assert report["restore_point"]["name"] == name
    assert report["restore_point"]["kind"] == "pre_upgrade"
    restore = next(
        finding for finding in report["findings"] if finding["name"] == "backup.restore_point"
    )
    assert restore["ok"] is True


def test_a_staging_name_is_attributed_to_the_cluster_that_wrote_it():
    from packages.agent_contracts.etcd_backup import staging_name_belongs

    assert staging_name_belongs("lab-20260102T000000Z.db.partial", cluster_name="lab")
    assert staging_name_belongs(
        "snapshot-pre-lab-v1.34.1-20260102T000000Z.db.partial", cluster_name="lab"
    )
    # Legacy upgrade snapshots name the version and not the cluster.
    assert staging_name_belongs(
        "snapshot-pre-v1.34.1-20260102T000000Z.db.partial", cluster_name="lab"
    )
    assert not staging_name_belongs("other-20260102T000000Z.db.partial", cluster_name="lab")
    assert not staging_name_belongs(
        "snapshot-pre-other-v1.34.1-20260102T000000Z.db.partial", cluster_name="lab"
    )
    assert not staging_name_belongs("notes.db.partial", cluster_name="lab")


def test_a_cluster_with_only_pre_upgrade_snapshots_reports_what_may_be_pruned():
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    entries = snapshot_entries(
        [
            {"name": f"snapshot-pre-lab-v1.34.{index}-{stamp}.db"}
            for index, stamp in (
                (1, "20261001T000000Z"),
                (2, "20261002T000000Z"),
                (3, "20261003T000000Z"),
            )
        ],
        cluster_name="lab",
    )

    report = assess(
        entries,
        keep_count=2,
        keep_max_age_hours=8760,
        max_age_hours=8760,
        pre_upgrade_keep_max_age_hours=168,
        now=now,
    )

    # No recurring backup exists, so the set is not healthy...
    assert report["ok"] is False
    # ...but the old pre-upgrade files are past their policy and a run would
    # delete them: this report must not claim there is nothing to prune.
    prunable = [entry["name"] for entry in report["prunable"]]
    assert prunable == [
        "snapshot-pre-lab-v1.34.1-20261001T000000Z.db",
        "snapshot-pre-lab-v1.34.2-20261002T000000Z.db",
    ]
