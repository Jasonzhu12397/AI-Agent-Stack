from uuid import uuid4

from apps.cloud_control_agent.deployment_controller import (
    advance_after_command,
    reconcile_deployments,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, *, deployment, command, agent_id, existing=None, active=False):
        self.deployment = deployment
        self.command = command
        self.agent_id = agent_id
        self.existing = existing
        self.active = active
        self.inserted = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if "SELECT 1 FROM agent_control_commands" in normalized:
            return _Result([{"active": True}] if self.active else [])
        if "FROM agent_control_deployments" in normalized:
            return _Result([self.deployment])
        if "WHERE command_id = %s AND deployment_id = %s" in normalized:
            return _Result([self.command] if self.command else [])
        if "status = 'succeeded'" in normalized:
            return _Result([self.command] if self.command else [])
        if "FROM agent_control_agents" in normalized:
            return _Result([{"agent_id": self.agent_id}] if self.agent_id else [])
        if "WHERE idempotency_key = %s" in normalized:
            return _Result([self.existing] if self.existing else [])
        if "INSERT INTO agent_control_commands" in normalized:
            self.inserted.append(params)
            return _Result([])
        return _Result([])


def _deployment(**overrides):
    document = {
        "deployment_id": uuid4(),
        "installation_id": uuid4(),
        "cluster_id": uuid4(),
        "phase": "eph_single_node_ready",
        "spec": {"cluster_name": "edge-01"},
        "state": {"evidence": {"preflight": True}},
    }
    document.update(overrides)
    return document


def _command(**overrides):
    document = {
        "command_id": uuid4(),
        "status": "succeeded",
        "action": "eph.preflight",
        "from_phase": "not_started",
        "next_phase": "eph_single_node_ready",
    }
    document.update(overrides)
    return document


def test_advance_queues_the_control_stack_after_checkpointed_preflight():
    deployment = _deployment()
    connection = _Connection(
        deployment=deployment,
        command=_command(next_phase=deployment["phase"]),
        agent_id=uuid4(),
    )
    result = advance_after_command(
        connection,
        deployment["deployment_id"],
        uuid4(),
    )
    assert result["status"] == "queued"
    assert result["action"] == "eph.controlstack.apply"
    assert result["from_phase"] == "eph_single_node_ready"
    assert result["next_phase"] == "control_stack_ready"
    assert connection.inserted


def test_advance_waits_when_the_checkpoint_has_not_advanced():
    deployment = _deployment()
    connection = _Connection(
        deployment=deployment,
        command=_command(next_phase="bmh_registered"),
        agent_id=uuid4(),
    )
    result = advance_after_command(connection, deployment["deployment_id"])
    assert result["status"] == "checkpoint_not_advanced"
    assert connection.inserted == []


def test_advance_does_not_duplicate_an_existing_planned_command():
    deployment = _deployment()
    connection = _Connection(
        deployment=deployment,
        command=_command(next_phase=deployment["phase"]),
        agent_id=uuid4(),
        existing={"command_id": uuid4(), "status": "queued"},
    )
    result = advance_after_command(connection, deployment["deployment_id"])
    assert result["status"] == "exists"
    assert connection.inserted == []


def test_advance_leaves_step_pending_when_no_agent_is_registered():
    deployment = _deployment()
    connection = _Connection(
        deployment=deployment,
        command=_command(next_phase=deployment["phase"]),
        agent_id=None,
    )
    result = advance_after_command(connection, deployment["deployment_id"])
    assert result["status"] == "no_eligible_agent"
    assert connection.inserted == []


def test_advance_stops_at_the_terminal_phase():
    deployment = _deployment(phase="completed")
    connection = _Connection(
        deployment=deployment,
        command=_command(next_phase="completed"),
        agent_id=uuid4(),
    )
    result = advance_after_command(connection, deployment["deployment_id"])
    assert result["status"] == "terminal"


def test_agent_registration_can_queue_the_first_step_without_a_prior_command():
    deployment = _deployment(phase="not_started", state={})
    connection = _Connection(
        deployment=deployment,
        command=None,
        agent_id=uuid4(),
    )
    result = advance_after_command(
        connection,
        deployment["deployment_id"],
        require_command=False,
    )
    assert result["status"] == "queued"
    assert result["action"] == "eph.preflight"
    assert result["next_phase"] == "eph_single_node_ready"


def test_reconcile_queues_a_deployment_that_has_no_active_command():
    deployment = _deployment(phase="not_started", state={})
    connection = _Connection(
        deployment=deployment,
        command=None,
        agent_id=uuid4(),
    )
    summary = reconcile_deployments(connection, limit=10)
    assert summary["scanned"] == 1
    assert summary["queued"] == 1
    assert summary["results"][0]["action"] == "eph.preflight"


def test_reconcile_skips_a_deployment_with_an_active_command():
    deployment = _deployment()
    connection = _Connection(
        deployment=deployment,
        command=_command(next_phase=deployment["phase"]),
        agent_id=uuid4(),
        active=True,
    )
    summary = reconcile_deployments(connection, limit=10)
    assert summary["queued"] == 0
    assert summary["results"][0]["status"] == "active_command"
    assert connection.inserted == []
