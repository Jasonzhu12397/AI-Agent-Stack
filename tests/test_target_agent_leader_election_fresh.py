from datetime import datetime, timedelta, timezone

from apps.target_agent.leader_election import LeaderLeaseGuard, KubernetesLeaseElector


def test_parse_time_accepts_kubernetes_rfc3339():
    value = KubernetesLeaseElector._parse_time("2026-11-17T12:00:00Z")
    assert value == datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def test_parse_time_rejects_invalid_value():
    assert KubernetesLeaseElector._parse_time("not-a-time") is None


def test_expiry_boundary_is_timezone_aware():
    renewed = datetime.now(timezone.utc) - timedelta(seconds=31)
    assert renewed + timedelta(seconds=30) < datetime.now(timezone.utc)



def test_leader_guard_marks_lost_when_another_replica_holds_lease():
    class FakeElector:
        def try_acquire_or_renew(self):
            return False

    guard = LeaderLeaseGuard(FakeElector())
    assert guard.renew_once() is False
    assert guard.lost.is_set()


def test_leader_guard_marks_lost_when_kubernetes_api_fails():
    class FakeElector:
        def try_acquire_or_renew(self):
            raise RuntimeError("api unavailable")

    guard = LeaderLeaseGuard(FakeElector())
    assert guard.renew_once() is False
    assert guard.lost.is_set()


def test_leader_guard_keeps_leadership_when_lease_renewal_succeeds():
    class FakeElector:
        def try_acquire_or_renew(self):
            return True

    guard = LeaderLeaseGuard(FakeElector())
    assert guard.renew_once() is True
    assert not guard.lost.is_set()
