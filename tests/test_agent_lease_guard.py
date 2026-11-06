from types import SimpleNamespace

import pytest

from worker_app import tasks


def test_agent_lease_guard_renews_with_claimed_identity(monkeypatch):
    calls = []
    task = SimpleNamespace(
        id="task-1",
        lease_owner="worker-1",
        lease_token="lease-token-1",
    )

    def renew(task_id, lease_token, worker_id):
        calls.append((task_id, lease_token, worker_id))
        return True

    monkeypatch.setattr(tasks, "renew_lease", renew)

    assert tasks._renew_agent_lease(task) is None
    assert calls == [("task-1", "lease-token-1", "worker-1")]


def test_agent_lease_guard_stops_when_lease_is_lost(monkeypatch):
    task = SimpleNamespace(
        id="task-2",
        lease_owner="worker-2",
        lease_token="lease-token-2",
    )
    monkeypatch.setattr(tasks, "renew_lease", lambda *args: False)

    with pytest.raises(tasks.PermanentTaskError, match="lease was lost"):
        tasks._renew_agent_lease(task)


def test_agent_lease_guard_rejects_missing_lease_identity(monkeypatch):
    called = []
    task = SimpleNamespace(id="task-3", lease_owner=None, lease_token=None)
    monkeypatch.setattr(tasks, "renew_lease", lambda *args: called.append(args))

    with pytest.raises(tasks.PermanentTaskError, match="lease identity is missing"):
        tasks._renew_agent_lease(task)

    assert called == []
