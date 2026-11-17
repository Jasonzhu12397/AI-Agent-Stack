from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.agent_contracts.models import (
    AgentIdentity,
    AgentRole,
    CommandEnvelope,
    CommandLease,
    CommandResult,
)


def test_target_identity_requires_cluster_scope():
    with pytest.raises(ValidationError):
        AgentIdentity(
            agent_id=uuid4(),
            role=AgentRole.TARGET,
            installation_id=uuid4(),
            instance_id="target-master-1",
        )


def test_eph_identity_rejects_target_cluster_scope():
    with pytest.raises(ValidationError):
        AgentIdentity(
            agent_id=uuid4(),
            role=AgentRole.EPH,
            installation_id=uuid4(),
            cluster_id=uuid4(),
            instance_id="eph-live-1",
        )


def test_lease_rejects_naive_timestamps():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        CommandLease(
            lease_id=uuid4(),
            token="x" * 32,
            issued_at=now.replace(tzinfo=None),
            expires_at=now + timedelta(minutes=1),
        )


def test_command_lease_must_not_outlive_deadline():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        CommandEnvelope(
            command_id=uuid4(),
            idempotency_key="idempotency-key-0001",
            deployment_id=uuid4(),
            target_agent_id=uuid4(),
            required_role=AgentRole.TARGET,
            action="worker.provision",
            payload={},
            issued_at=now,
            deadline=now + timedelta(minutes=5),
            lease=CommandLease(
                lease_id=uuid4(),
                token="x" * 32,
                issued_at=now,
                expires_at=now + timedelta(minutes=6),
            ),
        )


def test_command_rejects_unknown_fields():
    now = datetime.now(timezone.utc)
    data = {
        "command_id": uuid4(),
        "idempotency_key": "idempotency-key-0001",
        "deployment_id": uuid4(),
        "target_agent_id": uuid4(),
        "required_role": AgentRole.TARGET,
        "action": "cluster.observe",
        "payload": {},
        "issued_at": now,
        "deadline": now + timedelta(minutes=5),
        "lease": {
            "lease_id": uuid4(),
            "token": "x" * 32,
            "issued_at": now,
            "expires_at": now + timedelta(minutes=4),
        },
        "unexpected": True,
    }
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(data)


def test_failed_result_requires_error_code():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        CommandResult(
            command_id=uuid4(),
            agent_id=uuid4(),
            lease_id=uuid4(),
            status="failed",
            started_at=now,
            completed_at=now,
            side_effects=False,
        )
