from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from apps.target_agent import main as target_main


def _ready_deployment():
    return {
        "metadata": {"generation": 4},
        "spec": {"replicas": 3},
        "status": {
            "observedGeneration": 4,
            "updatedReplicas": 3,
            "availableReplicas": 3,
            "readyReplicas": 3,
        },
    }


def _lease(holder="target-agent-0", renew_time=None, duration=30):
    return {
        "spec": {
            "holderIdentity": holder,
            "renewTime": renew_time or datetime.now(UTC).isoformat(),
            "leaseDurationSeconds": duration,
        },
    }


def test_target_agent_readiness_uses_live_deployment_and_lease(monkeypatch):
    monkeypatch.setenv("AGENT_LEASE_NAMESPACE", "ai-agent-system")
    monkeypatch.setenv("AGENT_LEASE_NAME", "target-agent-leader")
    monkeypatch.setenv("AGENT_INSTANCE_ID", "target-agent-0")
    responses = iter([
        {"available": True, "stdout": json.dumps(_ready_deployment())},
        {"available": True, "stdout": json.dumps(_lease())},
    ])
    monkeypatch.setattr(target_main, "_kubectl", lambda *_args, **_kwargs: next(responses))
    evidence = target_main._observe_target_agent_readiness()
    assert evidence["target_agent_ready"] is True
    assert evidence["target_agent_leader_elected"] is True
    assert evidence["target_agent_replica_count"] == 3


def test_target_agent_readiness_rejects_unavailable_replica(monkeypatch):
    monkeypatch.setenv("AGENT_INSTANCE_ID", "target-agent-0")
    deployment = _ready_deployment()
    deployment["status"]["availableReplicas"] = 2
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda *_args, **_kwargs: {
            "available": True,
            "stdout": json.dumps(deployment),
        },
    )
    with pytest.raises(
        target_main.HandoffError,
        match="THREE_REPLICA_DEPLOYMENT_NOT_READY",
    ):
        target_main._observe_target_agent_readiness()


def test_target_agent_readiness_rejects_lease_owned_by_another_replica(monkeypatch):
    monkeypatch.setenv("AGENT_INSTANCE_ID", "target-agent-0")
    responses = iter([
        {"available": True, "stdout": json.dumps(_ready_deployment())},
        {"available": True, "stdout": json.dumps(_lease(holder="target-agent-1"))},
    ])
    monkeypatch.setattr(target_main, "_kubectl", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(
        target_main.HandoffError,
        match="LEADER_LEASE_NOT_HELD_OR_EXPIRED",
    ):
        target_main._observe_target_agent_readiness()
