"""Alerting must reach a human, say what happened, and never leak the target."""

from __future__ import annotations

import json

import pytest
from core import etcd_backup_alert as alert

REPORT = {
    "checked_at": "2026-11-22T00:00:00Z",
    "problems": [
        {
            "deployment_id": "d-1",
            "cluster_name": "lab",
            "code": "ETCD_BACKUP_STALE",
            "newest": "lab-20261120T000000Z.db",
            "newest_age_hours": 30.5,
            "restore_point": None,
            "scheduler_age_hours": 0.4,
            "last_run_status": "succeeded",
        },
        {
            "deployment_id": "d-2",
            "cluster_name": "edge",
            "code": "ETCD_BACKUP_NOT_RUN_YET",
            "newest": None,
            "newest_age_hours": None,
        },
    ],
}


def test_no_webhook_means_nothing_to_do(monkeypatch):
    monkeypatch.delenv(alert.WEBHOOK_ENV, raising=False)

    result = alert.deliver(REPORT, sender=_forbidden)

    assert result == {"configured": False, "delivered": False, "reason": "no_webhook"}


def _forbidden(*_args, **_kwargs):
    raise AssertionError("no POST was expected")


def test_a_healthy_report_does_not_page(monkeypatch):
    monkeypatch.setenv(alert.WEBHOOK_ENV, "https://hooks.example/etcd")

    result = alert.deliver({"checked_at": "now", "problems": []}, sender=_forbidden)

    assert result["configured"] is True
    assert result["delivered"] is False
    assert result["reason"] == "nothing_to_report"


def test_a_problem_is_posted_once_with_an_actionable_body(monkeypatch):
    monkeypatch.setenv(alert.WEBHOOK_ENV, "https://hooks.example/etcd")
    seen = []

    def sender(url, body, timeout):
        seen.append((url, json.loads(body), timeout))
        return 204

    result = alert.deliver(REPORT, sender=sender)

    assert result == {"configured": True, "delivered": True, "status": 204}
    assert len(seen) == 1
    url, payload, timeout = seen[0]
    assert url == "https://hooks.example/etcd"
    assert timeout == alert.DEFAULT_TIMEOUT_SECONDS
    assert payload["kind"] == "etcd_backup_problem"
    assert payload["problem_count"] == 2
    assert payload["problems"][0]["code"] == "ETCD_BACKUP_STALE"
    assert "lab: ETCD_BACKUP_STALE (newest backup 30.5h old)" in payload["text"]
    assert "edge: ETCD_BACKUP_NOT_RUN_YET (no backup ever reported)" in payload["text"]


def test_a_failure_to_send_is_reported_but_does_not_raise(monkeypatch):
    monkeypatch.setenv(alert.WEBHOOK_ENV, "https://hooks.example/etcd")

    def sender(url, body, timeout):
        raise TimeoutError("no answer")

    result = alert.deliver(REPORT, sender=sender)

    assert result["delivered"] is False
    assert result["reason"] == "send_failed"
    assert "TimeoutError" in result["detail"]


def test_a_non_2xx_answer_is_not_a_delivery(monkeypatch):
    monkeypatch.setenv(alert.WEBHOOK_ENV, "https://hooks.example/etcd")

    result = alert.deliver(REPORT, sender=lambda *_: 500)

    assert result["delivered"] is False
    assert result["reason"] == "http_status"
    assert result["status"] == 500


def test_the_target_is_never_echoed_back(monkeypatch):
    monkeypatch.setenv(alert.WEBHOOK_ENV, "https://hooks.example/secret-token")
    body = alert.alert_body(REPORT)

    assert b"secret-token" not in body
    result = alert.deliver(REPORT, sender=lambda *_: 200)
    assert "hooks.example" not in json.dumps(result)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", alert.DEFAULT_TIMEOUT_SECONDS),
        ("3", 3.0),
        ("0", alert.DEFAULT_TIMEOUT_SECONDS),
        ("x", alert.DEFAULT_TIMEOUT_SECONDS),
    ],
)
def test_the_timeout_is_configurable_and_sane(monkeypatch, raw, expected):
    monkeypatch.setenv(alert.TIMEOUT_ENV, raw)

    assert alert.timeout_seconds() == expected
