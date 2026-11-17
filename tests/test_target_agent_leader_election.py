import datetime
import time

import pytest

from apps.node_agent import agent


def _elector():
    return agent.KubernetesLeaseLeaderElector(
        namespace="ai-agent-system",
        lease_name="target-cluster-agent-leader",
        identity="target-cluster-agent-0",
    )


def _lease(holder, renew_time, resource_version="7", transitions=1):
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": "target-cluster-agent-leader",
            "namespace": "ai-agent-system",
            "resourceVersion": resource_version,
        },
        "spec": {
            "holderIdentity": holder,
            "leaseDurationSeconds": 30,
            "acquireTime": renew_time,
            "renewTime": renew_time,
            "leaseTransitions": transitions,
        },
    }


def test_target_agent_leader_election_creates_missing_lease(monkeypatch):
    calls = []

    def get_lease(path):
        raise agent.NodeAgentError("KUBERNETES_API_HTTP_404")

    def write_lease(method, path, payload):
        calls.append((method, path, payload))
        return {}

    monkeypatch.setattr(agent, "kubernetes_api_get", get_lease)
    monkeypatch.setattr(agent, "kubernetes_api_lease_request", write_lease)

    assert _elector().acquire_or_renew() is True
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/namespaces/ai-agent-system/leases")
    assert calls[0][2]["spec"]["holderIdentity"] == "target-cluster-agent-0"


def test_target_agent_standby_does_not_steal_active_lease(monkeypatch):
    now = datetime.datetime.now(datetime.UTC)
    renewed = now.isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: _lease("target-cluster-agent-1", renewed),
    )
    monkeypatch.setattr(
        agent,
        "kubernetes_api_lease_request",
        lambda *args: pytest.fail("active Lease must not be updated"),
    )

    assert _elector().acquire_or_renew() is False


def test_target_agent_takes_over_expired_lease_with_resource_version(monkeypatch):
    expired = (
        datetime.datetime.now(datetime.UTC)
        - datetime.timedelta(seconds=90)
    ).isoformat().replace("+00:00", "Z")
    calls = []
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: _lease("target-cluster-agent-1", expired, transitions=4),
    )
    monkeypatch.setattr(
        agent,
        "kubernetes_api_lease_request",
        lambda method, path, payload: calls.append((method, path, payload)) or {},
    )

    assert _elector().acquire_or_renew() is True
    assert calls[0][0] == "PUT"
    assert calls[0][2]["metadata"]["resourceVersion"] == "7"
    assert calls[0][2]["spec"]["holderIdentity"] == "target-cluster-agent-0"
    assert calls[0][2]["spec"]["leaseTransitions"] == 5


def test_target_agent_rejects_malformed_lease_holder(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: _lease({"unexpected": "object"}, "2026-11-17T00:00:00Z"),
    )

    with pytest.raises(
        agent.NodeAgentError,
        match="TARGET_AGENT_LEADER_LEASE_MALFORMED",
    ):
        _elector().acquire_or_renew()


def test_target_agent_standby_does_not_poll_control_plane(monkeypatch):
    class Client:
        deployment_checkpoints = {}

        def heartbeat(self, node_type):
            return {"active_deployments": []}

        def poll(self):
            pytest.fail("standby Target Agent must not poll commands")

    monkeypatch.setattr(
        agent,
        "inspect_host_network_hardware",
        lambda: {"status": "not_configured"},
    )
    monkeypatch.setattr(agent, "touch_health_files", lambda **kwargs: None)
    authority = agent.threading.Event()

    assert agent.run_once(Client(), "target_cluster", authority) is False


def test_target_agent_command_is_cancelled_after_leadership_loss(monkeypatch):
    authority = agent.threading.Event()
    authority.set()

    class Client:
        def renew_lease(self, command_id, lease_token):
            pytest.fail("leadership was lost before command lease renewal")

    def execute(command, node_type, cancellation_event=None):
        authority.clear()
        deadline = time.monotonic() + 3
        while not cancellation_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"unexpected_completion": not cancellation_event.is_set()}

    monkeypatch.setattr(agent, "execute_command", execute)

    with pytest.raises(agent.NodeAgentError, match="COMMAND_LEASE_LOST") as exc_info:
        agent.execute_command_with_lease(
            Client(),
            {"id": "command-1", "lease_token": "lease-token"},
            "target_cluster",
            renew_interval=30,
            authority_event=authority,
        )
    assert str(exc_info.value.__cause__) == "AGENT_LEADERSHIP_LOST"
