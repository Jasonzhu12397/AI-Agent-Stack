from datetime import datetime, timedelta, timezone

from apps.target_agent.leader_election import KubernetesLeaseElector


def test_parse_time_accepts_kubernetes_rfc3339():
    value = KubernetesLeaseElector._parse_time("2026-11-17T12:00:00Z")
    assert value == datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def test_parse_time_rejects_invalid_value():
    assert KubernetesLeaseElector._parse_time("not-a-time") is None


def test_expiry_boundary_is_timezone_aware():
    renewed = datetime.now(timezone.utc) - timedelta(seconds=31)
    assert renewed + timedelta(seconds=30) < datetime.now(timezone.utc)
