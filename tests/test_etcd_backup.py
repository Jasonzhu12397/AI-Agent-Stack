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
        validate_request({"cluster_name": "lab", "protect": ["x.db"]})
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
    assert names == ["snapshot_save", "snapshot_verify", "snapshot_publish", "retention"]
    publish = next(step for step in plan["steps"] if step["name"] == "snapshot_publish")
    assert "mv" in publish["commands"][0]
    assert ".partial" in plan["steps"][0]["commands"][0]
    assert plan["approval_required"] is False


# ---------------------------------------------------------------- executor


class FakeRunner:
    """Answers the four kubectl calls a backup run makes."""

    def __init__(self, *, fail_on: str = "", files: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.files = list(files or [])

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
    assert steps == ["prepare", "snapshot_save", "snapshot_verify", "snapshot_publish", "retention"]
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
    assert result["pruned"] == []
    assert [step["name"] for step in result["steps"]] == ["prepare", "snapshot_save"]
    assert not [call for call in runner.calls if "rm" in call]


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
