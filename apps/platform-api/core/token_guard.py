"""Token-budget guards attached to every ModelGateway call.

The gateway is the single metering entry point: it asks the guard to reserve
budget before issuing a provider request, settles the actual consumption on
success, and releases the reservation on failure. Two guard flavors exist:

* ``RunTokenGuard`` wraps a reservation owned by an ``AgentRun`` or
  ``WorkflowRun`` (the worker and workflow engine paths).
* ``AdHocTokenGuard`` meters calls without a run context, such as model
  connection tests and RAG embedding requests; the ledger row is tied to a
  free-form ``scope_ref``.
"""

import hashlib

from core.token_budget import (
    release_tenant_tokens,
    reserve_adhoc_tokens,
    reserve_tenant_tokens,
    settle_tenant_tokens,
)


class RunTokenGuard:
    """Budget guard for a provider call owned by an agent or workflow run."""

    def __init__(self, *, task, agent_run=None, workflow_run=None):
        if (agent_run is None) == (workflow_run is None):
            raise ValueError("Exactly one AgentRun or WorkflowRun must own a token guard")
        self.task = task
        self.agent_run = agent_run
        self.workflow_run = workflow_run

    @property
    def authorized(self) -> bool:
        return self.agent_run is not None or self.workflow_run is not None

    def reserve(self, *, idempotency_key: str, requested_tokens: int, **_kwargs):
        lease_token = getattr(self.task, "lease_token", None)
        if lease_token:
            lease_scoped_key = hashlib.sha256(
                f"{idempotency_key}:{lease_token}".encode()
            ).hexdigest()
        else:
            lease_scoped_key = idempotency_key
        return reserve_tenant_tokens(
            task=self.task,
            agent_run=self.agent_run,
            workflow_run=self.workflow_run,
            idempotency_key=lease_scoped_key,
            requested_tokens=requested_tokens,
        )

    def settle(self, *, reservation, consumed_tokens: int):
        return settle_tenant_tokens(usage=reservation, consumed_tokens=consumed_tokens)

    def release(self, *, reservation):
        return release_tenant_tokens(usage=reservation)


class AdHocTokenGuard:
    """Budget guard for provider calls without an agent/workflow run context."""

    def __init__(self, *, tenant, scope_ref: str):
        self.tenant = tenant
        self.scope_ref = scope_ref

    @property
    def authorized(self) -> bool:
        return self.tenant is not None and bool(self.scope_ref)

    def reserve(self, *, idempotency_key: str, requested_tokens: int, **_kwargs):
        return reserve_adhoc_tokens(
            tenant=self.tenant,
            scope_ref=self.scope_ref,
            idempotency_key=idempotency_key,
            requested_tokens=requested_tokens,
        )

    def settle(self, *, reservation, consumed_tokens: int):
        return settle_tenant_tokens(usage=reservation, consumed_tokens=consumed_tokens)

    def release(self, *, reservation):
        return release_tenant_tokens(usage=reservation)
