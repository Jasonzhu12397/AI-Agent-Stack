from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentRole(StrEnum):
    CLOUD_CONTROL = "cloud_control"
    EPH = "eph"
    TARGET = "target"


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    schema_version: Literal["v1"] = "v1"


class AgentIdentity(ContractModel):
    agent_id: UUID
    role: AgentRole
    installation_id: UUID
    cluster_id: UUID | None = None
    instance_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_scope(self) -> AgentIdentity:
        if self.role is AgentRole.TARGET and self.cluster_id is None:
            raise ValueError("target Agent identity requires cluster_id")
        if self.role is AgentRole.EPH and self.cluster_id is not None:
            raise ValueError("EPH Agent identity must not claim the target cluster_id")
        return self


class CommandLease(ContractModel):
    lease_id: UUID
    token: str = Field(min_length=32, max_length=512)
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> CommandLease:
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expires_at must be later than issued_at")
        return self


class CommandEnvelope(ContractModel):
    command_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=200)
    deployment_id: UUID
    target_agent_id: UUID
    required_role: AgentRole
    action: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    payload: dict[str, Any]
    issued_at: datetime
    deadline: datetime
    lease: CommandLease
    approval_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("issued_at", "deadline")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("command timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_command_window(self) -> CommandEnvelope:
        if self.deadline <= self.issued_at:
            raise ValueError("command deadline must be later than issued_at")
        if self.lease.issued_at < self.issued_at:
            raise ValueError("lease cannot be issued before its command")
        if self.lease.expires_at > self.deadline:
            raise ValueError("lease cannot outlive its command deadline")
        return self


class AgentHeartbeat(ContractModel):
    identity: AgentIdentity
    sequence: int = Field(ge=1)
    observed_at: datetime
    readiness: Literal["ready", "degraded", "not_ready"]
    capabilities: tuple[str, ...] = ()
    active_command_id: UUID | None = None

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("heartbeat timestamp must be timezone-aware")
        return value


class DeploymentCheckpoint(ContractModel):
    deployment_id: UUID
    cluster_id: UUID
    phase: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: dict[str, Any]
    recorded_at: datetime
    owner_agent_id: UUID

    @field_validator("recorded_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checkpoint timestamp must be timezone-aware")
        return value


class CommandResult(ContractModel):
    command_id: UUID
    agent_id: UUID
    lease_id: UUID
    status: Literal["succeeded", "failed", "cancelled", "blocked"]
    started_at: datetime
    completed_at: datetime
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    retryable: bool = False
    side_effects: bool
    evidence_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("result timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> CommandResult:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful results cannot include error_code")
        if self.status != "succeeded" and not self.error_code:
            raise ValueError("non-success results require error_code")
        return self
