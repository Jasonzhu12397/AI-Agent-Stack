import datetime
import hashlib
import json
import re
import uuid

from django.db import transaction
from django.utils import timezone

from core.models import (
    AgentCommand,
    ClusterDeployment,
    ClusterDeploymentCheckpoint,
    ClusterDeploymentPivotEvent,
    ClusterDeploymentPivotOperation,
)


def _payload_hash(command_type: str, payload: dict) -> str:
    encoded = json.dumps(
        {"command_type": command_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_capi_inventory(items: list[dict]) -> list[dict]:
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("CAPI_PIVOT_SOURCE_INVENTORY_INVALID")
        owner_references = item.get("owner_references")
        if not isinstance(owner_references, list):
            raise ValueError("CAPI_PIVOT_SOURCE_INVENTORY_INVALID")
        normalized_item = dict(item)
        normalized_item["owner_references"] = sorted(
            owner_references,
            key=lambda owner: (
                str(owner.get("api_version", "")),
                str(owner.get("kind", "")),
                str(owner.get("name", "")),
                str(owner.get("uid", "")),
                bool(owner.get("controller", False)),
            )
            if isinstance(owner, dict)
            else ("", "", "", "", False),
        )
        normalized.append(normalized_item)
    return sorted(
        normalized,
        key=lambda item: (
            str(item.get("resource", "")),
            str(item.get("namespace") or ""),
            str(item.get("name", "")),
            str(item.get("uid", "")),
        ),
    )


def _record_capi_pivot_event(
    deployment: ClusterDeployment,
    state: str,
    event_type: str,
    evidence: dict | None = None,
    actor_node=None,
    *,
    source_identity: dict | None = None,
    inventory: list | None = None,
    inventory_sha256: str | None = None,
    approval_hash: str | None = None,
    target_ack: dict | None = None,
    target_identity: dict | None = None,
    last_error: dict | None = None,
) -> ClusterDeploymentPivotOperation | None:
    operation = (
        ClusterDeploymentPivotOperation.objects.select_for_update()
        .filter(deployment=deployment)
        .first()
    )
    if operation is None:
        return None
    allowed = {
        ClusterDeploymentPivotOperation.State.PREPARED: {
            ClusterDeploymentPivotOperation.State.WAITING_APPROVAL,
            ClusterDeploymentPivotOperation.State.WAITING_TARGET_ACK,
            ClusterDeploymentPivotOperation.State.READY,
            ClusterDeploymentPivotOperation.State.FAILED,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.WAITING_APPROVAL: {
            ClusterDeploymentPivotOperation.State.READY,
            ClusterDeploymentPivotOperation.State.FAILED,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.WAITING_TARGET_ACK: {
            ClusterDeploymentPivotOperation.State.READY,
            ClusterDeploymentPivotOperation.State.FAILED,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.READY: {
            ClusterDeploymentPivotOperation.State.EXECUTING,
            ClusterDeploymentPivotOperation.State.FAILED,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.EXECUTING: {
            ClusterDeploymentPivotOperation.State.VERIFYING,
            ClusterDeploymentPivotOperation.State.UNCERTAIN,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.VERIFYING: {
            ClusterDeploymentPivotOperation.State.COMPLETED,
            ClusterDeploymentPivotOperation.State.UNCERTAIN,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.UNCERTAIN: {
            ClusterDeploymentPivotOperation.State.VERIFYING,
            ClusterDeploymentPivotOperation.State.COMPLETED,
            ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
        },
        ClusterDeploymentPivotOperation.State.COMPLETED: set(),
        ClusterDeploymentPivotOperation.State.FAILED: set(),
        ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY: set(),
    }
    if state != operation.state and state not in allowed.get(operation.state, set()):
        raise ValueError(
            f"Invalid CAPI pivot state transition: {operation.state} -> {state}"
        )
    previous_state = operation.state
    operation.state = state
    operation.version += 1
    if source_identity is not None:
        operation.source_identity = source_identity
    if inventory is not None:
        operation.inventory = inventory
    if inventory_sha256 is not None:
        operation.inventory_sha256 = inventory_sha256
    if approval_hash is not None:
        operation.approval_hash = approval_hash
    if target_ack is not None:
        operation.target_ack = target_ack
    if target_identity is not None:
        operation.target_identity = target_identity
    if last_error is not None:
        operation.last_error = last_error
    if state == ClusterDeploymentPivotOperation.State.EXECUTING and operation.started_at is None:
        operation.started_at = timezone.now()
    if state == ClusterDeploymentPivotOperation.State.COMPLETED:
        operation.completed_at = timezone.now()
    operation.journal = {
        **(operation.journal or {}),
        "last_event": event_type,
        "last_evidence": evidence or {},
        "last_transition_at": timezone.now().isoformat(),
    }
    operation.save(
        update_fields=[
            "state",
            "version",
            "source_identity",
            "inventory",
            "inventory_sha256",
            "approval_hash",
            "target_ack",
            "target_identity",
            "last_error",
            "started_at",
            "completed_at",
            "journal",
            "updated_at",
        ]
    )
    last_sequence = (
        operation.events.order_by("-sequence").values_list("sequence", flat=True).first()
        or 0
    )
    ClusterDeploymentPivotEvent.objects.create(
        operation=operation,
        sequence=last_sequence + 1,
        actor_node=actor_node,
        event_type=event_type,
        from_state=previous_state,
        to_state=state,
        evidence=evidence or {},
    )
    return operation


def _spec_hash(spec: dict) -> str:
    encoded = json.dumps(
        spec,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _advance_deployment_checkpoint(
    deployment: ClusterDeployment,
    agent_node,
    phase: str,
    completed_step: str,
    pending_step: str | None,
) -> None:
    phase_order = {
        "target_agent_bootstrap": 0,
        "capi_pivot_preflight": 1,
        "capi_pivot_verified": 2,
        "worker_provisioning": 3,
        "worker_pool_verified": 4,
        "eph_reclaim_pending": 5,
        "completed": 6,
    }
    if phase not in phase_order:
        raise ValueError("DEPLOYMENT_CHECKPOINT_PHASE_INVALID")
    if deployment.target_node_id != agent_node.id:
        raise ValueError("DEPLOYMENT_CHECKPOINT_AGENT_MISMATCH")
    checkpoint = ClusterDeploymentCheckpoint.objects.select_for_update().filter(
        deployment=deployment,
    ).first()
    if checkpoint is not None and (
        checkpoint.agent_node_id != agent_node.id or checkpoint.generation != 1
    ):
        raise ValueError("DEPLOYMENT_CHECKPOINT_OWNERSHIP_MISMATCH")
    data = checkpoint.data if checkpoint is not None else {}
    if checkpoint is not None:
        current_order = phase_order.get(checkpoint.phase)
        if current_order is None or phase_order[phase] < current_order:
            raise ValueError("DEPLOYMENT_CHECKPOINT_PHASE_REGRESSION")
    completed_steps = data.get("completed_steps", [])
    if not isinstance(completed_steps, list) or not all(
        isinstance(item, str) for item in completed_steps
    ):
        raise ValueError("DEPLOYMENT_CHECKPOINT_DATA_INVALID")
    completed_steps = list(completed_steps)
    if (
        checkpoint is not None
        and completed_step in completed_steps
        and checkpoint.phase == phase
        and data.get("pending_step") == pending_step
        and data.get("cluster_name") == deployment.cluster_name
    ):
        return
    if checkpoint is not None and checkpoint.phase == phase:
        raise ValueError("DEPLOYMENT_CHECKPOINT_SEQUENCE_CONFLICT")
    if completed_step not in completed_steps:
        completed_steps.append(completed_step)
    next_data = {
        "cluster_name": deployment.cluster_name,
        "completed_steps": completed_steps,
        "pending_step": pending_step,
    }
    if checkpoint is None:
        ClusterDeploymentCheckpoint.objects.create(
            deployment=deployment,
            agent_node=agent_node,
            generation=1,
            sequence=1,
            phase=phase,
            data=next_data,
        )
        return
    checkpoint.sequence += 1
    checkpoint.phase = phase
    checkpoint.data = next_data
    checkpoint.save(
        update_fields=["sequence", "phase", "data", "updated_at"],
    )


def _update_capi_pivot_provider_compatibility(deployment: ClusterDeployment) -> None:
    plan_result = deployment.plan_result or {}
    source = plan_result.get("capi_pivot_source_preflight", {})
    target = plan_result.get("capi_pivot_preflight", {})
    source_versions = source.get("provider_versions")
    target_versions = target.get("provider_versions")
    if not source.get("preflight_validated") or not target.get("preflight_validated"):
        return
    if not isinstance(source_versions, dict) or not isinstance(target_versions, dict):
        return
    groups = sorted(set(source_versions) | set(target_versions))
    mismatches = [
        {
            "group": group,
            "source": source_versions.get(group),
            "target": target_versions.get(group),
        }
        for group in groups
        if source_versions.get(group) != target_versions.get(group)
    ]
    deployment.plan_result = {
        **plan_result,
        "capi_pivot_provider_compatibility": {
            "checked": True,
            "basis": "served_api_versions_only",
            "compatible": not mismatches and len(groups) == 5,
            "mismatches": mismatches,
            "execution_allowed": False,
        },
    }
    deployment.save(update_fields=["plan_result", "updated_at"])






def _deployment_capi_namespace(deployment: ClusterDeployment) -> str:
    source = (deployment.plan_result or {}).get("capi_pivot_source_preflight", {})
    identity = source.get("source_cluster_identity", {}) if isinstance(source, dict) else {}
    namespace = identity.get("namespace") if isinstance(identity, dict) else None
    if isinstance(namespace, str) and namespace:
        return namespace
    configured = deployment.spec.get("capi_namespace", "default")
    return configured if isinstance(configured, str) and configured else "default"

@transaction.atomic
def _maybe_queue_capi_pivot(deployment: ClusterDeployment) -> None:
    deployment = ClusterDeployment.objects.select_for_update().get(pk=deployment.pk)
    if deployment.status == ClusterDeployment.Status.RECONCILIATION_REQUIRED:
        return
    raw_plan_result = deployment.plan_result
    if raw_plan_result is None:
        plan_result = {}
    elif isinstance(raw_plan_result, dict):
        plan_result = raw_plan_result
    else:
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {"code": "CAPI_PIVOT_PLAN_STATE_INVALID"}
        deployment.plan_result = {
            "dual_agent": {
                "phase": "pivot_plan_state_invalid",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            }
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    dual_agent_state = plan_result.get("dual_agent", {})
    if not isinstance(dual_agent_state, dict):
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {"code": "CAPI_PIVOT_DUAL_AGENT_STATE_INVALID"}
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                "phase": "pivot_dual_agent_state_invalid",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    source = plan_result.get("capi_pivot_source_preflight", {})
    target = plan_result.get("capi_pivot_preflight", {})
    compatibility = plan_result.get("capi_pivot_provider_compatibility", {})
    if (
        not isinstance(source, dict)
        or source.get("preflight_validated") is not True
        or not isinstance(target, dict)
        or target.get("preflight_validated") is not True
        or not isinstance(compatibility, dict)
        or deployment.target_node_id is None
    ):
        return
    if compatibility.get("compatible") is not True:
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **plan_result.get("dual_agent", {}),
                "phase": "provider_compatibility_blocked",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["plan_result", "updated_at"])
        return

    source_command = AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.pivot.source.preflight",
        node_id=deployment.eph_node_id,
        status=AgentCommand.Status.SUCCEEDED,
    ).order_by("-finished_at").first()
    target_command = AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.pivot.target.preflight",
        node_id=deployment.target_node_id,
        status=AgentCommand.Status.SUCCEEDED,
    ).order_by("-finished_at").first()
    now = timezone.now()
    if (
        source_command is None
        or source_command.finished_at is None
        or not isinstance(source_command.result, dict)
    ):
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **plan_result.get("dual_agent", {}),
                "phase": "pivot_source_preflight_missing",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["plan_result", "updated_at"])
        return
    if (
        target_command is None
        or target_command.finished_at is None
        or now - target_command.finished_at > datetime.timedelta(minutes=5)
        or not isinstance(target_command.result, dict)
    ):
        raw_refresh_count = dual_agent_state.get("target_preflight_refresh_count", 0)
        if (
            not isinstance(raw_refresh_count, int)
            or isinstance(raw_refresh_count, bool)
            or raw_refresh_count < 0
        ):
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.error = {
                "code": "CAPI_PIVOT_TARGET_PREFLIGHT_REFRESH_COUNTER_INVALID"
            }
            deployment.plan_result = {
                **plan_result,
                "dual_agent": {
                    **dual_agent_state,
                    "phase": "target_preflight_refresh_counter_invalid",
                    "target_agent_owns_cluster": False,
                    "eph_reclaim_allowed": False,
                    "manual_reconciliation_required": True,
                },
            }
            deployment.save(
                update_fields=["status", "error", "plan_result", "updated_at"]
            )
            return
        refresh_count = raw_refresh_count
        if refresh_count >= 3:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "CAPI_PIVOT_TARGET_ACK_REFRESH_LIMIT"}
            deployment.plan_result = {
                **plan_result,
                "dual_agent": {
                    **dual_agent_state,
                    "phase": "target_agent_ack_refresh_exhausted",
                    "target_agent_owns_cluster": False,
                    "eph_reclaim_allowed": False,
                },
            }
            deployment.save(
                update_fields=["status", "error", "plan_result", "updated_at"]
            )
            return
        refresh_payload = {
            "deployment_id": str(deployment.id),
            "cluster_name": deployment.cluster_name,
            "namespace": _deployment_capi_namespace(deployment),
        }
        refresh_key = (
            f"cluster-deployment:{deployment.id}:"
            f"cluster.capi.pivot.target.preflight:refresh:{refresh_count + 1}"
        )
        refresh_command, _ = AgentCommand.objects.get_or_create(
            tenant=deployment.tenant,
            idempotency_key=refresh_key,
            defaults={
                "project": deployment.project,
                "node": deployment.target_node,
                "requested_by": deployment.requested_by,
                "deployment": deployment,
                "command_type": "cluster.capi.pivot.target.preflight",
                "payload": refresh_payload,
                "status": AgentCommand.Status.QUEUED,
                "plan_hash": _payload_hash(
                    "cluster.capi.pivot.target.preflight", refresh_payload
                ),
            },
        )
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **dual_agent_state,
                "phase": "target_agent_ack_refresh_pending",
                "target_preflight_refresh_count": refresh_count + 1,
                "target_preflight_refresh_command_id": str(refresh_command.id),
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["plan_result", "updated_at"])
        return

    if (
        target_command.node_id != deployment.target_node_id
        or target_command.result.get("target_management_plane_ready") is not True
        or target_command.result.get("pivot_execution_allowed") is not True
        or target_command.result.get("missing_resources") != []
        or target_command.result.get("cluster_name_conflicts") != []
        or target_command.result.get("side_effects") is not False
    ):
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **plan_result.get("dual_agent", {}),
                "phase": "target_agent_preflight_not_ready",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["plan_result", "updated_at"])
        return

    source_identity = source.get("source_cluster_identity")
    source_inventory = source.get("inventory")
    if (
        not isinstance(source_identity, dict)
        or not isinstance(source_identity.get("uid"), str)
        or not source_identity["uid"]
        or not isinstance(source_identity.get("api_version"), str)
        or not isinstance(source_identity.get("spec_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", source_identity["spec_sha256"])
        or not isinstance(source_inventory, list)
        or source.get("inventory_truncated") is True
        or source.get("inventory_summary_truncated") is True
        or source.get("inventory_total_count") != len(source_inventory)
    ):
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **plan_result.get("dual_agent", {}),
                "phase": "pivot_source_inventory_incomplete",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["plan_result", "updated_at"])
        return

    target_ack = {
        "node_id": str(deployment.target_node_id),
        "command_id": str(target_command.id),
        "result_sha256": hashlib.sha256(
            json.dumps(
                target_command.result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "acknowledged_at": target_command.finished_at.isoformat(),
    }
    existing = AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.pivot.execute",
    ).first()
    payload = {
        "deployment_id": str(deployment.id),
        "cluster_name": deployment.cluster_name,
        "namespace": _deployment_capi_namespace(deployment),
        "operation_id": str(uuid.uuid4()),
        "source_cluster_uid": source_identity["uid"],
        "source_cluster_api_version": source_identity["api_version"],
        "source_cluster_spec_sha256": source_identity["spec_sha256"],
        "source_inventory_sha256": hashlib.sha256(
            json.dumps(
                _canonical_capi_inventory(source_inventory),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "target_agent_ack": target_ack,
    }
    expected_idempotency_key = (
        f"cluster-deployment:{deployment.id}:cluster.capi.pivot.execute"
    )
    if existing is not None:
        command = existing
    else:
        command, _ = AgentCommand.objects.get_or_create(
            tenant=deployment.tenant,
            idempotency_key=expected_idempotency_key,
            defaults={
                "project": deployment.project,
                "node": deployment.eph_node,
                "requested_by": deployment.requested_by,
                "deployment": deployment,
                "command_type": "cluster.capi.pivot.execute",
                "payload": payload,
                "status": AgentCommand.Status.WAITING_APPROVAL,
                "plan_hash": _payload_hash("cluster.capi.pivot.execute", payload),
            },
        )
    if (
        command.tenant_id != deployment.tenant_id
        or command.project_id != deployment.project_id
        or command.deployment_id != deployment.id
        or command.node_id != deployment.eph_node_id
        or command.command_type != "cluster.capi.pivot.execute"
        or command.idempotency_key != expected_idempotency_key
    ):
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {"code": "CAPI_PIVOT_COMMAND_IDENTITY_CONFLICT"}
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **dual_agent_state,
                "phase": "pivot_command_identity_conflict",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    persisted_payload = command.payload if isinstance(command.payload, dict) else {}
    operation_id = persisted_payload.get("operation_id")
    inventory_digest = persisted_payload.get("source_inventory_sha256")
    try:
        operation_uuid = uuid.UUID(operation_id) if isinstance(operation_id, str) else None
    except (TypeError, ValueError, AttributeError):
        operation_uuid = None
    if (
        operation_uuid is None
        or not isinstance(inventory_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", inventory_digest)
        or not isinstance(persisted_payload.get("target_agent_ack"), dict)
    ):
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {"code": "CAPI_PIVOT_COMMAND_PAYLOAD_INVALID"}
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **dual_agent_state,
                "phase": "pivot_command_payload_invalid",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    persisted_ack = persisted_payload["target_agent_ack"]
    operation, operation_created = ClusterDeploymentPivotOperation.objects.get_or_create(
        deployment=deployment,
        defaults={
            "operation_id": operation_uuid,
            "source_node": deployment.eph_node,
            "target_node": deployment.target_node,
            "state": ClusterDeploymentPivotOperation.State.PREPARED,
            "source_identity": source_identity,
            "inventory": source_inventory,
            "inventory_sha256": inventory_digest,
            "approval_hash": command.plan_hash,
            "target_ack": persisted_ack,
            "journal": {"created_from_command_id": str(command.id)},
        },
    )
    if (
        operation.operation_id != operation_uuid
        or operation.inventory_sha256 != inventory_digest
        or operation.source_node_id != deployment.eph_node_id
        or operation.target_node_id != deployment.target_node_id
    ):
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {"code": "CAPI_PIVOT_OPERATION_IDENTITY_CONFLICT"}
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **plan_result.get("dual_agent", {}),
                "phase": "pivot_operation_identity_conflict",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    state = ClusterDeploymentPivotOperation.State
    command_state = command.status
    expected_states = {
        state.WAITING_APPROVAL: {AgentCommand.Status.WAITING_APPROVAL},
        state.READY: {AgentCommand.Status.QUEUED},
        state.EXECUTING: {AgentCommand.Status.RUNNING},
        state.COMPLETED: {AgentCommand.Status.SUCCEEDED},
        state.FAILED: {AgentCommand.Status.FAILED},
        state.MANUAL_RECOVERY: {
            AgentCommand.Status.FAILED,
            AgentCommand.Status.CANCELLED,
        },
    }
    if operation_created:
        if command_state == AgentCommand.Status.WAITING_APPROVAL:
            _record_capi_pivot_event(
                deployment,
                state.WAITING_APPROVAL,
                "target_agent_acknowledged_approval_required",
                {
                    "source_inventory_sha256": inventory_digest,
                    "target_ack": persisted_ack,
                    "command_id": str(command.id),
                    "approval_required": True,
                },
                actor_node=deployment.target_node,
                source_identity=source_identity,
                inventory=source_inventory,
                inventory_sha256=inventory_digest,
                approval_hash=command.plan_hash,
                target_ack=persisted_ack,
            )
        elif command_state in {
            AgentCommand.Status.QUEUED,
            AgentCommand.Status.RUNNING,
        }:
            _record_capi_pivot_event(
                deployment,
                state.WAITING_APPROVAL,
                "pivot_command_recovered_after_approval",
                {"command_id": str(command.id), "command_status": command_state},
                actor_node=deployment.target_node,
                source_identity=source_identity,
                inventory=source_inventory,
                inventory_sha256=inventory_digest,
                approval_hash=command.approved_plan_hash or command.plan_hash,
                target_ack=persisted_ack,
            )
            _record_capi_pivot_event(
                deployment,
                state.READY,
                "pivot_approval_already_granted",
                {
                    "command_id": str(command.id),
                    "approved_by": str(command.approved_by_id) if command.approved_by_id else None,
                    "approved_at": command.approved_at.isoformat() if command.approved_at else None,
                },
                actor_node=deployment.target_node,
            )
            if command_state == AgentCommand.Status.RUNNING:
                _record_capi_pivot_event(
                    deployment,
                    state.EXECUTING,
                    "pivot_command_already_running",
                    {"command_id": str(command.id)},
                    actor_node=deployment.eph_node,
                )
        else:
            _record_capi_pivot_event(
                deployment,
                state.MANUAL_RECOVERY,
                "pivot_command_journal_gap_requires_reconciliation",
                {"command_id": str(command.id), "command_status": command_state},
                actor_node=deployment.eph_node,
                last_error={"code": "CAPI_PIVOT_COMMAND_JOURNAL_GAP"},
            )
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.error = {"code": "CAPI_PIVOT_COMMAND_JOURNAL_GAP"}
            deployment.plan_result = {
                **plan_result,
                "dual_agent": {
                    **dual_agent_state,
                    "phase": "pivot_command_journal_gap",
                    "target_agent_owns_cluster": False,
                    "eph_reclaim_allowed": False,
                    "manual_reconciliation_required": True,
                },
            }
            deployment.save(
                update_fields=["status", "error", "plan_result", "updated_at"]
            )
            return
    elif operation.state == state.READY and command_state == AgentCommand.Status.RUNNING:
        _record_capi_pivot_event(
            deployment,
            state.EXECUTING,
            "pivot_command_started",
            {"command_id": str(command.id)},
            actor_node=deployment.eph_node,
        )
    elif operation.state == state.WAITING_APPROVAL and command_state in {
        AgentCommand.Status.QUEUED,
        AgentCommand.Status.RUNNING,
    }:
        _record_capi_pivot_event(
            deployment,
            state.READY,
            "pivot_approval_granted",
            {
                "command_id": str(command.id),
                "approved_by": str(command.approved_by_id) if command.approved_by_id else None,
                "approved_at": command.approved_at.isoformat() if command.approved_at else None,
            },
            actor_node=deployment.target_node,
        )
        if command_state == AgentCommand.Status.RUNNING:
            _record_capi_pivot_event(
                deployment,
                state.EXECUTING,
                "pivot_command_started",
                {"command_id": str(command.id)},
                actor_node=deployment.eph_node,
            )
    elif command_state not in expected_states.get(operation.state, set()):
        _record_capi_pivot_event(
            deployment,
            (
                state.MANUAL_RECOVERY
                if operation.state in {
                    state.PREPARED,
                    state.WAITING_APPROVAL,
                    state.WAITING_TARGET_ACK,
                    state.READY,
                    state.EXECUTING,
                    state.VERIFYING,
                    state.UNCERTAIN,
                }
                else operation.state
            ),
            "pivot_operation_command_state_conflict",
            {
                "operation_state": operation.state,
                "command_status": command_state,
                "command_id": str(command.id),
            },
            actor_node=deployment.target_node,
            last_error={"code": "CAPI_PIVOT_OPERATION_COMMAND_STATE_CONFLICT"},
        )
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {"code": "CAPI_PIVOT_OPERATION_COMMAND_STATE_CONFLICT"}
        deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **dual_agent_state,
                "phase": "pivot_operation_command_state_conflict",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    deployment.plan_result = {
            **plan_result,
            "dual_agent": {
                **dual_agent_state,
                "phase": "pivot_operation_state_conflict",
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
        return
    if operation_created:
        _record_capi_pivot_event(
            deployment,
            ClusterDeploymentPivotOperation.State.WAITING_APPROVAL,
            "target_agent_acknowledged_approval_required",
            {
                "source_inventory_sha256": persisted_payload["source_inventory_sha256"],
                "target_ack": persisted_ack,
                "command_id": str(command.id),
                "approval_required": True,
            },
            actor_node=deployment.target_node,
            source_identity=source_identity,
            inventory=source_inventory,
            inventory_sha256=persisted_payload["source_inventory_sha256"],
            approval_hash=command.plan_hash,
            target_ack=persisted_ack,
        )
    operation.refresh_from_db()
    deployment.plan_result = {
        **plan_result,
        "dual_agent": {
            **plan_result.get("dual_agent", {}),
            "phase": (
                "pivot_waiting_approval"
                if operation.state == state.WAITING_APPROVAL
                else "pivot_approved_waiting_execution"
                if operation.state == state.READY
                else "pivot_execution_in_progress"
                if operation.state == state.EXECUTING
                else f"pivot_{operation.state}"
            ),
            "source_agent_id": str(deployment.eph_node_id),
            "target_agent_id": str(deployment.target_node_id),
            "pivot_command_id": str(command.id),
            "pivot_operation_id": persisted_payload.get("operation_id"),
            "target_agent_ack": persisted_ack,
            "approval_required": True,
            "target_agent_owns_cluster": False,
            "eph_reclaim_allowed": False,
        },
    }
    deployment.save(update_fields=["plan_result", "updated_at"])

def queue_deployment_command(
    deployment: ClusterDeployment,
    command_type: str,
    node=None,
) -> AgentCommand:
    payload = {"deployment_id": str(deployment.id)}
    if command_type == "cluster.postflight":
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = _deployment_capi_namespace(deployment)
        payload["expected_worker_replicas"] = deployment.spec.get("worker_replicas")
        worker_config = deployment.spec.get("worker_provisioning")
        if isinstance(worker_config, dict):
            payload["machine_deployment_name"] = worker_config.get(
                "machine_deployment_name"
            )
    elif command_type == "cluster.capi.pivot.target.preflight":
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = _deployment_capi_namespace(deployment)
    elif command_type == "cluster.capi.pivot.source.preflight":
        payload["cluster_name"] = deployment.cluster_name
    elif command_type == "cluster.capi.pivot.execute":
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = _deployment_capi_namespace(deployment)
    elif command_type == "cluster.capi.pivot.target.verify":
        source = (deployment.plan_result or {}).get("capi_pivot_source_preflight", {})
        identity = source.get("source_cluster_identity", {})
        dual_agent = (deployment.plan_result or {}).get("dual_agent", {})
        namespace = _deployment_capi_namespace(deployment)
        operation = ClusterDeploymentPivotOperation.objects.filter(
            deployment=deployment
        ).first()
        if operation is None or not isinstance(operation.inventory, list):
            raise ValueError("CAPI_PIVOT_DURABLE_INVENTORY_REQUIRED")
        expected_inventory = _canonical_capi_inventory(
            [
                item
                for item in operation.inventory
                if isinstance(item, dict) and item.get("namespace") == namespace
            ]
        )
        if not expected_inventory:
            raise ValueError("CAPI_PIVOT_NAMESPACE_INVENTORY_EMPTY")
        expected_inventory_sha256 = hashlib.sha256(
            json.dumps(
                expected_inventory,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = namespace
        payload["expected_cluster_uid"] = identity.get("uid")
        payload["expected_cluster_api_version"] = identity.get("api_version")
        payload["expected_cluster_spec_sha256"] = identity.get("spec_sha256")
        payload["expected_target_cluster_uid"] = dual_agent.get(
            "target_cluster_uid_after_move"
        )
        payload["operation_id"] = dual_agent.get("pivot_operation_id")
        payload["expected_inventory"] = expected_inventory
        payload["expected_inventory_sha256"] = expected_inventory_sha256
    elif command_type == "cluster.capi.pivot.postflight":
        target_verification = (deployment.plan_result or {}).get(
            "capi_pivot_target_verification",
            {},
        )
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = _deployment_capi_namespace(deployment)
        payload["expected_cluster_uid"] = target_verification.get("cluster_uid")
        worker_config = deployment.spec.get("worker_provisioning")
        host_namespace = (
            worker_config.get("baremetal_host_namespace")
            if isinstance(worker_config, dict)
            else None
        )
        payload["baremetal_host_namespace"] = (
            host_namespace
            if isinstance(host_namespace, str) and host_namespace
            else deployment.spec.get(
                "baremetal_host_namespace",
                _deployment_capi_namespace(deployment),
            )
        )
    elif command_type == "cluster.capi.worker.preflight":
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = _deployment_capi_namespace(deployment)
        payload["expected_worker_replicas"] = deployment.spec.get("worker_replicas")
        payload["worker_provisioning"] = deployment.spec.get("worker_provisioning")
    elif command_type == "cluster.capi.worker.provision":
        preflight = (deployment.plan_result or {}).get(
            "worker_provisioning_preflight", {}
        )
        payload.update(
            {
                "cluster_name": deployment.cluster_name,
                "namespace": _deployment_capi_namespace(deployment),
                "expected_worker_replicas": deployment.spec.get("worker_replicas"),
                "kubernetes_version": deployment.kubernetes_version,
                "worker_provisioning": deployment.spec.get("worker_provisioning"),
                "expected_cluster_uid": preflight.get("cluster_uid"),
                "expected_cluster_api_version": (
                    "cluster.x-k8s.io/" + preflight.get("provider_versions", {}).get(
                        "cluster.x-k8s.io", ""
                    )
                ),
                "expected_infrastructure_template_uid": preflight.get(
                    "infrastructure_template", {}
                ).get("uid"),
                "expected_bootstrap_template_uid": preflight.get(
                    "bootstrap_template", {}
                ).get("uid"),
                "infrastructure_api_version": preflight.get(
                    "infrastructure_template", {}
                ).get("api_version"),
                "bootstrap_api_version": preflight.get(
                    "bootstrap_template", {}
                ).get("api_version"),
            }
        )
        payload["provisioning_plan_hash"] = _spec_hash(payload)
    else:
        payload["cluster_spec"] = deployment.spec
    idempotency_key = f"cluster-deployment:{deployment.id}:{command_type}"
    command, _ = AgentCommand.objects.get_or_create(
        tenant=deployment.tenant,
        idempotency_key=idempotency_key,
        defaults={
            "project": deployment.project,
            "node": node or deployment.eph_node,
            "requested_by": deployment.requested_by,
            "deployment": deployment,
            "command_type": command_type,
            "payload": payload,
            "status": (
                AgentCommand.Status.WAITING_APPROVAL
                if command_type in {
                    "cluster.capi.pivot.execute",
                    "cluster.capi.worker.provision",
                }
                else AgentCommand.Status.QUEUED
            ),
            "plan_hash": _payload_hash(command_type, payload),
        },
    )
    expected_node = node or deployment.eph_node
    expected_hash = _payload_hash(command_type, payload)
    if (
        command.tenant_id != deployment.tenant_id
        or command.project_id != deployment.project_id
        or command.node_id != expected_node.id
        or command.deployment_id != deployment.id
        or command.command_type != command_type
        or command.idempotency_key != idempotency_key
        or command.payload != payload
        or command.plan_hash != expected_hash
    ):
        current_plan = (
            deployment.plan_result
            if isinstance(deployment.plan_result, dict)
            else {}
        )
        current_dual_agent = current_plan.get("dual_agent", {})
        if not isinstance(current_dual_agent, dict):
            current_dual_agent = {}
        deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
        deployment.error = {
            "code": "DEPLOYMENT_COMMAND_IDENTITY_CONFLICT",
            "command_type": command_type,
            "command_id": str(command.id),
        }
        deployment.plan_result = {
            **current_plan,
            "dual_agent": {
                **current_dual_agent,
                "phase": "deployment_command_identity_conflict",
                "target_agent_owns_cluster": (
                    current_dual_agent.get("target_agent_owns_cluster") is True
                ),
                "eph_reclaim_allowed": False,
                "manual_reconciliation_required": True,
            },
        }
        deployment.save(
            update_fields=["status", "error", "plan_result", "updated_at"]
        )
    return command


def handle_deployment_command_result(
    command: AgentCommand,
    outcome: str,
    result: dict,
    error: dict | None,
) -> None:
    if not isinstance(result, dict):
        result = {}
    if command.deployment_id is None:
        return
    deployment = ClusterDeployment.objects.select_for_update().get(
        pk=command.deployment_id,
    )
    if deployment.status in {
        ClusterDeployment.Status.CANCELLED,
        ClusterDeployment.Status.SUCCEEDED,
        ClusterDeployment.Status.FAILED,
        ClusterDeployment.Status.RECONCILIATION_REQUIRED,
    }:
        return
    if deployment.started_at is None:
        deployment.started_at = timezone.now()
    if outcome == "failed":
        if command.command_type in {
            "cluster.capi.pivot.target.verify",
            "cluster.capi.pivot.postflight",
        }:
            postflight = command.command_type == "cluster.capi.pivot.postflight"
            _record_capi_pivot_event(
                deployment,
                ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
                "target_agent_verification_failed",
                {
                    "command_id": str(command.id),
                    "command_type": command.command_type,
                    "error": error or {"code": "AGENT_COMMAND_FAILED"},
                },
                actor_node=command.node,
                last_error=error or {"code": "AGENT_COMMAND_FAILED"},
            )
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.plan_result = {
                **(deployment.plan_result or {}),
                "capi_pivot_reconciliation": {
                    "state": "target_verification_outcome_unknown",
                    "command_id": str(command.id),
                    "error": error or {"code": "AGENT_COMMAND_FAILED"},
                    "side_effects": "unknown",
                    "retry_allowed": False,
                    "eph_reclaim_allowed": False,
                },
                "dual_agent": {
                    **((deployment.plan_result or {}).get("dual_agent", {})),
                    "phase": (
                        "pivot_postflight_outcome_unknown"
                        if postflight
                        else "target_adoption_outcome_unknown"
                    ),
                    "target_agent_owns_cluster": postflight,
                    "eph_reclaim_allowed": False,
                    "manual_reconciliation_required": True,
                },
            }
            deployment.error = {
                "code": "CAPI_PIVOT_TARGET_VERIFICATION_OUTCOME_UNKNOWN",
                "command_id": str(command.id),
                "retry_allowed": False,
            }
            deployment.save(
                update_fields=[
                    "status",
                    "plan_result",
                    "error",
                    "started_at",
                    "updated_at",
                ],
            )
            return
        if command.command_type == "cluster.capi.pivot.execute":
            _record_capi_pivot_event(
                deployment,
                ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
                "eph_agent_execution_failed",
                {
                    "command_id": str(command.id),
                    "operation_id": (
                        command.payload.get("operation_id")
                        if isinstance(command.payload, dict)
                        else None
                    ),
                    "error": error or {"code": "AGENT_COMMAND_FAILED"},
                },
                actor_node=command.node,
                last_error=error or {"code": "AGENT_COMMAND_FAILED"},
            )
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.plan_result = {
                **(deployment.plan_result or {}),
                "capi_pivot_reconciliation": {
                    "state": "outcome_unknown",
                    "command_id": str(command.id),
                    "operation_id": (
                        command.payload.get("operation_id")
                        if isinstance(command.payload, dict)
                        else None
                    ),
                    "error": error or {"code": "AGENT_COMMAND_FAILED"},
                    "side_effects": "unknown",
                    "retry_allowed": False,
                    "eph_reclaim_allowed": False,
                },
                "dual_agent": {
                    **((deployment.plan_result or {}).get("dual_agent", {})),
                    "phase": "pivot_outcome_unknown",
                    "pivot_completed": False,
                    "target_agent_owns_cluster": False,
                    "eph_reclaim_allowed": False,
                    "manual_reconciliation_required": True,
                },
            }
            deployment.error = {
                "code": "CAPI_PIVOT_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED",
                "command_id": str(command.id),
                "retry_allowed": False,
            }
            deployment.save(
                update_fields=[
                    "status",
                    "plan_result",
                    "error",
                    "started_at",
                    "updated_at",
                ],
            )
            return
        if command.command_type == "cluster.capi.worker.provision":
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.plan_result = {
                **(deployment.plan_result or {}),
                "worker_provisioning_reconciliation": {
                    "state": "outcome_unknown",
                    "command_id": str(command.id),
                    "error": error or {"code": "AGENT_COMMAND_FAILED"},
                    "side_effects": "unknown",
                    "retry_allowed": False,
                    "eph_reclaim_allowed": False,
                },
                "dual_agent": {
                    **((deployment.plan_result or {}).get("dual_agent", {})),
                    "worker_provisioning_completed": False,
                    "worker_provisioning_simulated": False,
                    "eph_reclaim_allowed": False,
                },
            }
            deployment.error = {
                "code": "WORKER_PROVISIONING_OUTCOME_UNKNOWN",
                "reconciliation_required": True,
                "retry_allowed": False,
            }
            deployment.save(
                update_fields=[
                    "status",
                    "plan_result",
                    "error",
                    "started_at",
                    "updated_at",
                ],
            )
            return
        deployment.status = ClusterDeployment.Status.FAILED
        deployment.error = error or {"code": "AGENT_COMMAND_FAILED"}
        deployment.finished_at = timezone.now()
        deployment.save(
            update_fields=[
                "status",
                "error",
                "started_at",
                "finished_at",
                "updated_at",
            ],
        )
        return
    if command.command_type == "host.preflight":
        cpu_count = result.get("cpu_count")
        if (
            command.node.node_type != "eph_node"
            or not isinstance(cpu_count, int)
            or isinstance(cpu_count, bool)
            or cpu_count < 1
        ):
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "INVALID_EPH_PREFLIGHT_RESULT"}
            deployment.finished_at = timezone.now()
            deployment.save(
                update_fields=[
                    "status",
                    "error",
                    "started_at",
                    "finished_at",
                    "updated_at",
                ],
            )
            return
        deployment.preflight_result = result
        deployment.status = ClusterDeployment.Status.PLANNING
        deployment.save(
            update_fields=["preflight_result", "status", "started_at", "updated_at"],
        )
        queue_deployment_command(deployment, "cluster.plan")
    elif command.command_type == "cluster.capi.pivot.source.preflight":
        controllers = result.get("controllers")
        missing_resources = result.get("missing_resources")
        pagination_required = result.get("pagination_required")
        blockers = result.get("blockers")
        provider_versions = result.get("provider_versions")
        resource_counts = result.get("resource_counts")
        inventory = result.get("inventory")
        source_cluster_identity = result.get("source_cluster_identity")
        owner_graph = result.get("owner_graph")
        inventory_total_count = result.get("inventory_total_count")
        inventory_summary_truncated = result.get("inventory_summary_truncated")
        expected_controllers = {
            ("capi-system", "capi-controller-manager"),
            (
                "capi-kubeadm-bootstrap-system",
                "capi-kubeadm-bootstrap-controller-manager",
            ),
            (
                "capi-kubeadm-control-plane-system",
                "capi-kubeadm-control-plane-controller-manager",
            ),
            ("capm3-system", "capm3-controller-manager"),
        }
        expected_provider_groups = {
            "cluster.x-k8s.io",
            "bootstrap.cluster.x-k8s.io",
            "controlplane.cluster.x-k8s.io",
            "infrastructure.cluster.x-k8s.io",
            "metal3.io",
        }
        expected_resource_keys = {
            "cluster.x-k8s.io/clusters",
            "cluster.x-k8s.io/machines",
            "cluster.x-k8s.io/machinesets",
            "cluster.x-k8s.io/machinedeployments",
            "bootstrap.cluster.x-k8s.io/kubeadmconfigs",
            "bootstrap.cluster.x-k8s.io/kubeadmconfigtemplates",
            "controlplane.cluster.x-k8s.io/kubeadmcontrolplanes",
            "infrastructure.cluster.x-k8s.io/metal3clusters",
            "infrastructure.cluster.x-k8s.io/metal3machines",
            "infrastructure.cluster.x-k8s.io/metal3machinetemplates",
            "metal3.io/baremetalhosts",
        }
        valid_controllers = (
            isinstance(controllers, list)
            and len(controllers) == len(expected_controllers)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("namespace"), str)
                and isinstance(item.get("deployment"), str)
                and isinstance(item.get("ready"), bool)
                for item in controllers
            )
            and {
                (item["namespace"], item["deployment"])
                for item in controllers
                if isinstance(item, dict)
                and isinstance(item.get("namespace"), str)
                and isinstance(item.get("deployment"), str)
            }
            == expected_controllers
        )
        valid_lists = all(
            isinstance(items, list)
            and len(items) <= 100
            and all(isinstance(item, str) and len(item) <= 200 for item in items)
            for items in [missing_resources, pagination_required, blockers]
        )
        valid_versions = (
            isinstance(provider_versions, dict)
            and set(provider_versions) == expected_provider_groups
            and all(
                isinstance(version, str)
                and bool(re.fullmatch(r"[A-Za-z0-9.]+", version))
                for version in provider_versions.values()
            )
        )
        safe_resource_counts = (
            resource_counts if isinstance(resource_counts, dict) else {}
        )
        valid_counts = (
            isinstance(resource_counts, dict)
            and set(resource_counts) == expected_resource_keys
            and all(
                isinstance(key, str)
                and len(key) <= 160
                and isinstance(count, int)
                and not isinstance(count, bool)
                and 0 <= count <= 100000
                for key, count in resource_counts.items()
            )
        )
        valid_inventory = (
            isinstance(inventory, list)
            and 1 <= len(inventory) <= 200
            and all(
                isinstance(item, dict)
                and isinstance(item.get("api_version"), str)
                and isinstance(item.get("kind"), str)
                and isinstance(item.get("resource"), str)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("uid"), str)
                and bool(item.get("uid"))
                and isinstance(item.get("owner_references_truncated"), bool)
                and (
                    item.get("namespace") is None
                    or isinstance(item.get("namespace"), str)
                )
                and isinstance(item.get("owner_references"), list)
                and len(item.get("owner_references")) <= 10
                and item.get("resource") in safe_resource_counts
                and safe_resource_counts.get(item.get("resource"), 0) > 0
                for item in inventory
            )
            and len(inventory) <= sum(safe_resource_counts.values())
        )
        valid_source_cluster_identity = (
            isinstance(source_cluster_identity, dict)
            and source_cluster_identity.get("name") == deployment.cluster_name
            and (
                not deployment.spec.get("capi_namespace")
                or source_cluster_identity.get("namespace")
                == deployment.spec.get("capi_namespace")
            )
            and isinstance(source_cluster_identity.get("namespace"), str)
            and bool(source_cluster_identity.get("namespace"))
            and isinstance(source_cluster_identity.get("uid"), str)
            and bool(source_cluster_identity.get("uid"))
            and isinstance(source_cluster_identity.get("api_version"), str)
            and isinstance(source_cluster_identity.get("spec_sha256"), str)
            and bool(re.fullmatch(
                r"[a-f0-9]{64}",
                source_cluster_identity.get("spec_sha256", ""),
            ))
            and isinstance(inventory, list)
            and any(
                isinstance(item, dict)
                and item.get("resource") == "cluster.x-k8s.io/clusters"
                and item.get("name") == source_cluster_identity.get("name")
                and item.get("namespace") == source_cluster_identity.get("namespace")
                and item.get("uid") == source_cluster_identity.get("uid")
                and item.get("api_version") == source_cluster_identity.get("api_version")
                and item.get("spec_sha256") == source_cluster_identity.get("spec_sha256")
                for item in inventory
            )
        )
        valid_owner_graph = (
            isinstance(owner_graph, dict)
            and owner_graph.get("complete") is True
            and isinstance(source_cluster_identity, dict)
            and owner_graph.get("root_cluster_uid") == source_cluster_identity.get("uid")
            and isinstance(inventory_total_count, int)
            and not isinstance(inventory_total_count, bool)
            and 1 <= inventory_total_count <= 100000
            and inventory_total_count == sum(safe_resource_counts.values())
            and inventory_summary_truncated == (len(inventory) < inventory_total_count)
            and owner_graph.get("object_count") == inventory_total_count
            and owner_graph.get("unique_uid_count") == inventory_total_count
            and isinstance(inventory_summary_truncated, bool)
            and len(inventory) <= inventory_total_count
            and isinstance(owner_graph.get("reachable_object_count"), int)
            and not isinstance(owner_graph.get("reachable_object_count"), bool)
            and 1 <= owner_graph.get("reachable_object_count") <= inventory_total_count
            and isinstance(owner_graph.get("cluster_labeled_object_count"), int)
            and not isinstance(owner_graph.get("cluster_labeled_object_count"), bool)
            and 0 <= owner_graph.get("cluster_labeled_object_count") <= inventory_total_count
            and all(
                isinstance(owner_graph.get(key), list) and not owner_graph.get(key)
                for key in [
                    "duplicate_uids",
                    "truncated_owner_references",
                    "unresolved_owner_references",
                    "orphaned_cluster_objects",
                ]
            )
        )
        preflight_valid = (
            command.node.node_type == "eph_node"
            and command.node_id == deployment.eph_node_id
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("side_effects") is False
            and result.get("inventory_truncated") is False
            and isinstance(result.get("source_management_plane_ready"), bool)
            and valid_controllers
            and valid_lists
            and valid_versions
            and valid_counts
            and valid_inventory
            and valid_source_cluster_identity
            and valid_owner_graph
        )
        if not preflight_valid:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "INVALID_CAPI_SOURCE_PREFLIGHT_RESULT"}
            deployment.finished_at = timezone.now()
            deployment.save(
                update_fields=[
                    "status",
                    "error",
                    "started_at",
                    "finished_at",
                    "updated_at",
                ],
            )
            return
        ready = (
            result["source_management_plane_ready"] is True
            and not missing_resources
            and not pagination_required
            and not blockers
            and owner_graph["complete"] is True
            and all(item["ready"] for item in controllers)
        )
        current_dual_agent = (deployment.plan_result or {}).get("dual_agent", {})
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "capi_pivot_source_preflight": {
                **result,
                "preflight_validated": ready,
            },
            "dual_agent": {
                **current_dual_agent,
                "phase": "target_preflight_pending" if ready else "source_preflight_blocked",
                "source_preflight_validated": ready,
            },
        }
        deployment.error = None
        deployment.save(
            update_fields=["plan_result", "error", "started_at", "updated_at"],
        )
        _update_capi_pivot_provider_compatibility(deployment)
        if ready and deployment.target_node_id and not AgentCommand.objects.filter(
            deployment=deployment,
            command_type="cluster.capi.pivot.target.preflight",
        ).exists():
            queue_deployment_command(
                deployment,
                "cluster.capi.pivot.target.preflight",
                node=deployment.target_node,
            )
        _maybe_queue_capi_pivot(deployment)
    elif command.command_type == "cluster.capi.pivot.target.preflight":
        controllers = result.get("controllers")
        missing_resources = result.get("missing_resources")
        conflicts = result.get("cluster_name_conflicts")
        blockers = result.get("blockers")
        provider_versions = result.get("provider_versions")
        expected_provider_groups = {
            "cluster.x-k8s.io",
            "bootstrap.cluster.x-k8s.io",
            "controlplane.cluster.x-k8s.io",
            "infrastructure.cluster.x-k8s.io",
            "metal3.io",
        }
        valid_versions = (
            isinstance(provider_versions, dict)
            and set(provider_versions) == expected_provider_groups
            and all(
                isinstance(version, str)
                and bool(re.fullmatch(r"[A-Za-z0-9.]+", version))
                for version in provider_versions.values()
            )
        )
        expected_namespace = command.payload.get("namespace", "default")
        expected_controllers = {
            ("capi-system", "capi-controller-manager"),
            (
                "capi-kubeadm-bootstrap-system",
                "capi-kubeadm-bootstrap-controller-manager",
            ),
            (
                "capi-kubeadm-control-plane-system",
                "capi-kubeadm-control-plane-controller-manager",
            ),
            ("capm3-system", "capm3-controller-manager"),
        }
        valid_controllers = (
            isinstance(controllers, list)
            and len(controllers) == len(expected_controllers)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("namespace"), str)
                and isinstance(item.get("deployment"), str)
                and isinstance(item.get("ready"), bool)
                for item in controllers
            )
            and {
                (item["namespace"], item["deployment"])
                for item in controllers
                if isinstance(item, dict)
                and isinstance(item.get("namespace"), str)
                and isinstance(item.get("deployment"), str)
            }
            == expected_controllers
        )
        valid_lists = all(
            isinstance(items, list)
            and len(items) <= 100
            and all(isinstance(item, str) and len(item) <= 200 for item in items)
            for items in [missing_resources, conflicts, blockers]
        )
        preflight_valid = (
            command.node.node_type == "target_cluster"
            and command.node_id == deployment.target_node_id
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == expected_namespace
            and isinstance(result.get("target_management_plane_ready"), bool)
            and isinstance(result.get("pivot_execution_allowed"), bool)
            and result.get("side_effects") is False
            and valid_controllers
            and valid_lists
            and valid_versions
        )
        if not preflight_valid:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "INVALID_CAPI_PIVOT_PREFLIGHT_RESULT"}
            deployment.finished_at = timezone.now()
            deployment.save(
                update_fields=[
                    "status",
                    "error",
                    "started_at",
                    "finished_at",
                    "updated_at",
                ],
            )
            return
        ready = (
            result["target_management_plane_ready"] is True
            and result["pivot_execution_allowed"] is True
            and not missing_resources
            and not conflicts
            and not blockers
            and all(item["ready"] for item in controllers)
        )
        current_dual_agent = (deployment.plan_result or {}).get("dual_agent", {})
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "capi_pivot_preflight": {
                **result,
                "preflight_validated": ready,
            },
            "dual_agent": {
                **current_dual_agent,
                "phase": "pivot_preflight_blocked" if not ready else "provider_compatibility_check",
                "target_preflight_validated": ready,
            },
        }
        deployment.error = None
        deployment.save(
            update_fields=["plan_result", "error", "started_at", "updated_at"],
        )
        _update_capi_pivot_provider_compatibility(deployment)
        _maybe_queue_capi_pivot(deployment)
    elif command.command_type == "cluster.capi.pivot.execute":
        expected_payload = command.payload if isinstance(command.payload, dict) else {}
        target_ack = expected_payload.get("target_agent_ack")
        expected_ack = (deployment.plan_result or {}).get("dual_agent", {}).get(
            "target_agent_ack"
        )
        pivot_operation = ClusterDeploymentPivotOperation.objects.select_for_update().filter(
            deployment=deployment
        ).first()
        valid_execution = (
            pivot_operation is not None
            and str(pivot_operation.operation_id) == expected_payload.get("operation_id")
            and pivot_operation.state == ClusterDeploymentPivotOperation.State.EXECUTING
            and pivot_operation.inventory_sha256
            == expected_payload.get("source_inventory_sha256")
            and pivot_operation.approval_hash == command.approved_plan_hash
            and pivot_operation.target_ack == target_ack == expected_ack
            and command.status == AgentCommand.Status.SUCCEEDED
            and command.approved_at is not None
            and command.approved_by_id is not None
            and command.approved_by_id != command.requested_by_id
            and command.approved_plan_hash == command.plan_hash
            and command.plan_hash == _payload_hash(command.command_type, command.payload)
            and command.node.node_type == "eph_node"
            and command.node_id == deployment.eph_node_id
            and result.get("pivot_executed") is True
            and result.get("pivot_simulated") is False
            and result.get("execution_mode") == "clusterctl_move"
            and result.get("operation_id") == expected_payload.get("operation_id")
            and result.get("deployment_id") == str(deployment.id)
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == _deployment_capi_namespace(deployment)
            and result.get("source_cluster_uid") == expected_payload.get("source_cluster_uid")
            and result.get("source_cluster_spec_sha256")
            == expected_payload.get("source_cluster_spec_sha256")
            and result.get("source_inventory_sha256")
            == expected_payload.get("source_inventory_sha256")
            and result.get("target_agent_acknowledged") is True
            and result.get("target_agent_ack") == target_ack == expected_ack
            and result.get("source_cluster_absent") is True
            and result.get("source_inventory_empty") is True
            and result.get("target_cluster_present") is True
            and isinstance(result.get("target_cluster_uid"), str)
            and bool(result.get("target_cluster_uid"))
            and result.get("target_cluster_uid") != result.get("source_cluster_uid")
            and result.get("ownership_transferred") is False
            and result.get("target_agent_postflight_required") is True
            and result.get("side_effects") is True
            and result.get("automatic_retry_allowed") is False
        )
        if not valid_execution:
            _record_capi_pivot_event(
                deployment,
                ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
                "pivot_execution_result_rejected",
                {"command_id": str(command.id), "result": result},
                actor_node=command.node,
                last_error={"code": "CAPI_PIVOT_EXECUTION_RESULT_REJECTED"},
            )
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.error = {"code": "CAPI_PIVOT_EXECUTION_RESULT_REJECTED"}
            deployment.finished_at = timezone.now()
            deployment.plan_result = {
                **(deployment.plan_result or {}),
                "dual_agent": {
                    **((deployment.plan_result or {}).get("dual_agent", {})),
                    "phase": "pivot_execution_result_rejected",
                    "target_agent_owns_cluster": False,
                    "eph_reclaim_allowed": False,
                },
            }
            deployment.save(
                update_fields=[
                    "status", "error", "finished_at", "plan_result", "started_at", "updated_at"
                ],
            )
            return
        _record_capi_pivot_event(
            deployment,
            ClusterDeploymentPivotOperation.State.VERIFYING,
            "clusterctl_move_completed",
            {
                "command_id": str(command.id),
                "operation_id": result.get("operation_id"),
                "source_cluster_uid": result.get("source_cluster_uid"),
                "target_cluster_uid": result.get("target_cluster_uid"),
                "source_inventory_sha256": result.get("source_inventory_sha256"),
            },
            actor_node=command.node,
            target_identity={
                "uid": result.get("target_cluster_uid"),
                "api_version": expected_payload.get("source_cluster_api_version"),
                "spec_sha256": expected_payload.get("source_cluster_spec_sha256"),
            },
        )
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "capi_pivot_execution": result,
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "phase": "pivot_move_completed_target_verification_pending",
                "pivot_simulated": False,
                "pivot_move_completed": True,
                "pivot_completed": False,
                "target_cluster_uid_after_move": result["target_cluster_uid"],
                "target_agent_owns_cluster": False,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.error = None
        deployment.status = ClusterDeployment.Status.PROVISIONING
        deployment.save(
            update_fields=["plan_result", "error", "status", "started_at", "updated_at"],
        )
        queue_deployment_command(
            deployment,
            "cluster.capi.pivot.target.verify",
            node=deployment.target_node,
        )
    elif command.command_type == "cluster.capi.pivot.target.verify":
        source = (deployment.plan_result or {}).get("capi_pivot_source_preflight", {})
        source_identity = source.get("source_cluster_identity", {})
        expected_uid = source_identity.get("uid")
        expected_spec_sha256 = source_identity.get("spec_sha256")
        dual_agent = (deployment.plan_result or {}).get("dual_agent", {})
        expected_target_uid = dual_agent.get("target_cluster_uid_after_move")
        operation_id = dual_agent.get("pivot_operation_id")
        pivot_operation = ClusterDeploymentPivotOperation.objects.select_for_update().filter(
            deployment=deployment
        ).first()
        target_cluster_uid = result.get("cluster_uid")
        expected_inventory = _canonical_capi_inventory(
            [
                item
                for item in pivot_operation.inventory
                if isinstance(item, dict)
                and item.get("namespace") == _deployment_capi_namespace(deployment)
            ]
            if pivot_operation is not None
            and isinstance(pivot_operation.inventory, list)
            else []
        )
        expected_inventory_sha256 = hashlib.sha256(
            json.dumps(
                expected_inventory,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        valid_verification = (
            pivot_operation is not None
            and str(pivot_operation.operation_id) == operation_id
            and pivot_operation.state == ClusterDeploymentPivotOperation.State.VERIFYING
            and isinstance(pivot_operation.target_identity, dict)
            and pivot_operation.target_identity.get("uid") == expected_target_uid
            and isinstance(expected_uid, str)
            and bool(expected_uid)
            and isinstance(expected_spec_sha256, str)
            and bool(re.fullmatch(r"[a-f0-9]{64}", expected_spec_sha256))
            and isinstance(target_cluster_uid, str)
            and bool(target_cluster_uid)
            and command.node.node_type == "target_cluster"
            and command.node_id == deployment.target_node_id
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == _deployment_capi_namespace(deployment)
            and isinstance(expected_target_uid, str)
            and bool(expected_target_uid)
            and target_cluster_uid == expected_target_uid
            and target_cluster_uid != expected_uid
            and isinstance(operation_id, str)
            and result.get("operation_id") == operation_id
            and result.get("cluster_identity_preserved") is True
            and result.get("cluster_uid") != expected_uid
            and result.get("source_cluster_uid") == expected_uid
            and result.get("cluster_api_version") == source_identity.get("api_version")
            and result.get("cluster_spec_sha256") == expected_spec_sha256
            and expected_inventory
            and result.get("inventory_verified") is True
            and result.get("inventory_sha256") == expected_inventory_sha256
            and result.get("inventory_object_count") == len(expected_inventory)
            and result.get("pivot_adoption_verified") is True
            and result.get("machine_inventory_ready") is True
            and result.get("controllers_ready") is True
            and isinstance(result.get("machine_count"), int)
            and not isinstance(result.get("machine_count"), bool)
            and result.get("machine_count") > 0
            and isinstance(result.get("ready_machine_count"), int)
            and not isinstance(result.get("ready_machine_count"), bool)
            and result.get("ready_machine_count") == result.get("machine_count")
            and result.get("machine_inventory_ready") is True
            and result.get("not_ready_machine_names") == []
            and result.get("side_effects") is False
        )
        if not valid_verification:
            _record_capi_pivot_event(
                deployment,
                ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
                "target_agent_adoption_verification_failed",
                {"command_id": str(command.id), "result": result},
                actor_node=command.node,
                last_error={"code": "TARGET_AGENT_CAPI_ADOPTION_VERIFICATION_FAILED"},
            )
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.error = {"code": "TARGET_AGENT_CAPI_ADOPTION_VERIFICATION_FAILED"}
            deployment.finished_at = timezone.now()
            deployment.plan_result = {
                **(deployment.plan_result or {}),
                "dual_agent": {
                    **((deployment.plan_result or {}).get("dual_agent", {})),
                    "phase": "target_adoption_verification_failed",
                    "target_agent_owns_cluster": False,
                    "eph_reclaim_allowed": False,
                    "manual_reconciliation_required": True,
                },
            }
            deployment.save(
                update_fields=[
                    "status", "error", "finished_at", "plan_result", "started_at", "updated_at"
                ],
            )
            return
        _record_capi_pivot_event(
            deployment,
            ClusterDeploymentPivotOperation.State.VERIFYING,
            "target_agent_adopted_cluster",
            {
                "command_id": str(command.id),
                "operation_id": operation_id,
                "target_cluster_uid": target_cluster_uid,
                "machine_count": result.get("machine_count"),
                "ready_machine_count": result.get("ready_machine_count"),
            },
            actor_node=command.node,
            target_identity={
                "uid": target_cluster_uid,
                "api_version": result.get("cluster_api_version"),
                "spec_sha256": result.get("cluster_spec_sha256"),
            },
        )
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "capi_pivot_target_verification": result,
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "phase": "target_agent_adopted",
                "pivot_completed": True,
                "target_agent_owns_cluster": True,
                "target_agent_cluster_uid": target_cluster_uid,
                "source_cluster_uid": expected_uid,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["plan_result", "started_at", "updated_at"])
        deployment.status = ClusterDeployment.Status.PROVISIONING
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "phase": "pivot_metal3_ironic_postflight_pending",
                "target_agent_owns_cluster": True,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.save(update_fields=["status", "plan_result", "updated_at"])
        queue_deployment_command(
            deployment,
            "cluster.capi.pivot.postflight",
            node=deployment.target_node,
        )
    elif command.command_type == "cluster.capi.pivot.postflight":
        target_verification = (deployment.plan_result or {}).get(
            "capi_pivot_target_verification",
            {},
        )
        observation = result.get("metal3_observation")
        ironic = observation.get("ironic") if isinstance(observation, dict) else None
        machine_count = observation.get("machine_count") if isinstance(observation, dict) else None
        resolved_count = (
            observation.get("resolved_machine_count")
            if isinstance(observation, dict)
            else None
        )
        ironic_applicable = (
            ironic.get("applicable_node_count") if isinstance(ironic, dict) else None
        )
        ironic_verified = (
            ironic.get("verified_node_count") if isinstance(ironic, dict) else None
        )
        pivot_operation = ClusterDeploymentPivotOperation.objects.select_for_update().filter(
            deployment=deployment
        ).first()
        valid_postflight = (
            pivot_operation is not None
            and pivot_operation.state == ClusterDeploymentPivotOperation.State.VERIFYING
            and isinstance(pivot_operation.target_identity, dict)
            and pivot_operation.target_identity.get("uid")
            == target_verification.get("cluster_uid")
            and command.node.node_type == "target_cluster"
            and command.node_id == deployment.target_node_id
            and result.get("postflight_passed") is True
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == _deployment_capi_namespace(deployment)
            and isinstance(target_verification.get("cluster_uid"), str)
            and bool(target_verification.get("cluster_uid"))
            and result.get("cluster_uid") == target_verification.get("cluster_uid")
            and result.get("side_effects") is False
            and result.get("complete_hardware_verification") is True
            and result.get("verification_scope") == "capi_metal3_ironic"
            and result.get("ironic_verification") == "verified"
            and isinstance(observation, dict)
            and observation.get("ready") is True
            and isinstance(machine_count, int)
            and not isinstance(machine_count, bool)
            and machine_count > 0
            and isinstance(resolved_count, int)
            and not isinstance(resolved_count, bool)
            and resolved_count == machine_count
            and isinstance(ironic, dict)
            and ironic.get("status") == "verified"
            and isinstance(ironic_applicable, int)
            and not isinstance(ironic_applicable, bool)
            and ironic_applicable > 0
            and isinstance(ironic_verified, int)
            and not isinstance(ironic_verified, bool)
            and ironic_verified == ironic_applicable
        )
        if not valid_postflight:
            _record_capi_pivot_event(
                deployment,
                ClusterDeploymentPivotOperation.State.MANUAL_RECOVERY,
                "metal3_ironic_postflight_failed",
                {"command_id": str(command.id), "result": result},
                actor_node=command.node,
                last_error={"code": "CAPI_PIVOT_METAL3_IRONIC_POSTFLIGHT_FAILED"},
            )
            deployment.status = ClusterDeployment.Status.RECONCILIATION_REQUIRED
            deployment.error = {"code": "CAPI_PIVOT_METAL3_IRONIC_POSTFLIGHT_FAILED"}
            deployment.finished_at = timezone.now()
            deployment.plan_result = {
                **(deployment.plan_result or {}),
                "capi_pivot_postflight": result,
                "dual_agent": {
                    **((deployment.plan_result or {}).get("dual_agent", {})),
                    "phase": "pivot_postflight_failed",
                    "target_agent_owns_cluster": True,
                    "eph_reclaim_allowed": False,
                },
            }
            deployment.save(
                update_fields=[
                    "status",
                    "error",
                    "finished_at",
                    "plan_result",
                    "started_at",
                    "updated_at",
                ],
            )
            return
        _record_capi_pivot_event(
            deployment,
            ClusterDeploymentPivotOperation.State.COMPLETED,
            "metal3_ironic_postflight_verified",
            {
                "command_id": str(command.id),
                "cluster_uid": result.get("cluster_uid"),
                "ironic_verification": result.get("ironic_verification"),
                "complete_hardware_verification": result.get("complete_hardware_verification"),
            },
            actor_node=command.node,
            target_identity={
                "uid": result.get("cluster_uid"),
                "api_version": (
                    deployment.plan_result or {}
                ).get("capi_pivot_target_verification", {}).get("cluster_api_version"),
                "spec_sha256": (
                    deployment.plan_result or {}
                ).get("capi_pivot_target_verification", {}).get("cluster_spec_sha256"),
            },
        )
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "capi_pivot_postflight": result,
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "phase": "pivot_postflight_verified",
                "target_agent_owns_cluster": True,
                "eph_reclaim_allowed": False,
            },
        }
        expected_workers = deployment.spec.get("worker_replicas")
        worker_config = deployment.spec.get("worker_provisioning")
        if (
            isinstance(expected_workers, int)
            and not isinstance(expected_workers, bool)
            and expected_workers > 0
            and isinstance(worker_config, dict)
        ):
            deployment.status = ClusterDeployment.Status.PROVISIONING
            deployment.plan_result["dual_agent"]["phase"] = "worker_preflight_pending"
            deployment.save(update_fields=["status", "plan_result", "updated_at"])
            queue_deployment_command(
                deployment,
                "cluster.capi.worker.preflight",
                node=deployment.target_node,
            )
        elif isinstance(expected_workers, int) and expected_workers > 0:
            deployment.status = ClusterDeployment.Status.WAITING_APPROVAL
            deployment.error = {"code": "WORKER_PROVISIONING_SPEC_REQUIRED"}
            deployment.plan_result["dual_agent"]["phase"] = "worker_provisioning_spec_required"
            deployment.save(update_fields=["status", "error", "plan_result", "updated_at"])
        else:
            deployment.save(update_fields=["plan_result", "started_at", "updated_at"])
            queue_deployment_command(
                deployment,
                "cluster.postflight",
                node=deployment.target_node,
            )
    elif command.command_type == "cluster.capi.worker.preflight":
        config = deployment.spec.get("worker_provisioning")
        expected_workers = deployment.spec.get("worker_replicas")
        source = (deployment.plan_result or {}).get("capi_pivot_source_preflight", {})
        source_identity = source.get("source_cluster_identity", {})
        expected_uid = source_identity.get("uid")
        infrastructure = result.get("infrastructure_template")
        bootstrap = result.get("bootstrap_template")
        hosts = result.get("selected_baremetal_hosts")
        network = result.get("network_profile")
        versions = result.get("provider_versions")
        valid_result = (
            command.node.node_type == "target_cluster"
            and command.node_id == deployment.target_node_id
            and isinstance(config, dict)
            and command.payload.get("worker_provisioning") == config
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == _deployment_capi_namespace(deployment)
            and isinstance(expected_uid, str)
            and bool(expected_uid)
            and result.get("expected_worker_replicas") == expected_workers
            and isinstance(expected_workers, int)
            and not isinstance(expected_workers, bool)
            and expected_workers > 0
            and result.get("cluster_uid") == expected_uid
            and result.get("ready") is True
            and result.get("machine_deployment_exists") is False
            and result.get("execution_allowed") is False
            and result.get("worker_creation_performed") is False
            and result.get("side_effects") is False
            and result.get("controllers_ready") is True
            and isinstance(result.get("controllers"), list)
            and len(result.get("controllers")) == 4
            and all(
                isinstance(controller, dict)
                and controller.get("ready") is True
                and isinstance(controller.get("namespace"), str)
                and isinstance(controller.get("deployment"), str)
                for controller in result.get("controllers", [])
            )
            and {
                (controller.get("namespace"), controller.get("deployment"))
                for controller in result.get("controllers", [])
                if isinstance(controller, dict)
            } == {
                ("capi-system", "capi-controller-manager"),
                ("capi-kubeadm-bootstrap-system", "capi-kubeadm-bootstrap-controller-manager"),
                (
                    "capi-kubeadm-control-plane-system",
                    "capi-kubeadm-control-plane-controller-manager",
                ),
                ("capm3-system", "capm3-controller-manager"),
            }
            and result.get("baremetal_host_namespace")
            == config.get("baremetal_host_namespace")
            and result.get("host_selector") == config.get("host_selector")
            and result.get("template_host_selector_verified") is True
            and result.get("template_host_selector") == config.get("host_selector")
            and result.get("blockers") == []
            and isinstance(result.get("available_baremetal_host_count"), int)
            and not isinstance(result.get("available_baremetal_host_count"), bool)
            and result.get("available_baremetal_host_count") >= expected_workers
            and isinstance(hosts, list)
            and len(hosts) >= expected_workers
            and all(
                isinstance(host, dict)
                and isinstance(host.get("name"), str)
                and host.get("name")
                and isinstance(host.get("uid"), str)
                and host.get("uid")
                and host.get("ready") is True
                for host in hosts[:expected_workers]
            )
            and len({host.get("name") for host in hosts if isinstance(host, dict)}) == len(hosts)
            and len({host.get("uid") for host in hosts if isinstance(host, dict)}) == len(hosts)
            and isinstance(infrastructure, dict)
            and infrastructure.get("name") == config.get("infrastructure_template_name")
            and isinstance(infrastructure.get("uid"), str)
            and infrastructure.get("uid")
            and isinstance(infrastructure.get("api_version"), str)
            and infrastructure.get("api_version")
            and isinstance(bootstrap, dict)
            and bootstrap.get("name") == config.get("bootstrap_template_name")
            and isinstance(bootstrap.get("uid"), str)
            and bootstrap.get("uid")
            and isinstance(bootstrap.get("api_version"), str)
            and bootstrap.get("api_version")
            and isinstance(network, dict)
            and network.get("valid") is True
            and network.get("pod_traffic_default") == "data_plane"
            and network.get("primary_cni") == "calico"
            and network.get("meta_cni") == "multus"
            and network.get("dpdk_requested") is True
            and isinstance(network.get("attachment_count"), int)
            and network.get("attachment_count") > 0
            and network.get("profile_hash") == _spec_hash(config.get("network_profile", {}))
            and result.get("network_template_profile_verified") is True
            and result.get("network_template_profile_hash") == network.get("profile_hash")
            and isinstance(versions, dict)
            and set(versions) == {
                "cluster.x-k8s.io",
                "bootstrap.cluster.x-k8s.io",
                "infrastructure.cluster.x-k8s.io",
                "metal3.io",
            }
            and all(isinstance(value, str) and value for value in versions.values())
        )
        if not valid_result:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "TARGET_WORKER_PROVISIONING_PREFLIGHT_FAILED"}
            deployment.finished_at = timezone.now()
            deployment.save(update_fields=["status", "error", "finished_at", "updated_at"])
            return
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "worker_provisioning_preflight": result,
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "phase": "worker_preflight_passed_waiting_provisioning_approval",
                "target_agent_owns_cluster": True,
                "worker_preflight_validated": True,
                "eph_reclaim_allowed": False,
            },
        }
        _advance_deployment_checkpoint(
            deployment,
            deployment.target_node,
            "worker_provisioning",
            "worker_provisioning_preflight",
            "worker_provisioning",
        )
        deployment.status = ClusterDeployment.Status.WAITING_APPROVAL
        deployment.error = None
        provision_command = queue_deployment_command(
            deployment,
            "cluster.capi.worker.provision",
            node=deployment.target_node,
        )
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "worker_provisioning_command_id": str(provision_command.id),
                "worker_provisioning_approval_required": True,
            },
        }
        deployment.save(update_fields=["plan_result", "status", "error", "updated_at"])
    elif command.command_type == "cluster.capi.worker.provision":
        expected = command.payload
        source = (deployment.plan_result or {}).get("capi_pivot_source_preflight", {})
        source_identity = (
            source.get("source_cluster_identity", {})
            if isinstance(source, dict)
            else {}
        )
        approved_payload = dict(expected) if isinstance(expected, dict) else {}
        approved_provisioning_hash = approved_payload.pop(
            "provisioning_plan_hash", None
        )
        valid_result = (
            isinstance(expected, dict)
            and isinstance(source_identity, dict)
            and command.status == AgentCommand.Status.SUCCEEDED
            and command.approved_at is not None
            and command.approved_by_id is not None
            and command.approved_by_id != command.requested_by_id
            and command.approved_plan_hash == command.plan_hash
            and command.plan_hash == _payload_hash(command.command_type, expected)
            and approved_provisioning_hash == _spec_hash(approved_payload)
            and expected.get("expected_cluster_uid") == source_identity.get("uid")
            and expected.get("expected_worker_replicas") == deployment.spec.get("worker_replicas")
            and expected.get("worker_provisioning") == deployment.spec.get("worker_provisioning")
            and command.node.node_type == "target_cluster"
            and command.node_id == deployment.target_node_id
            and result.get("deployment_id") == str(deployment.id)
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == _deployment_capi_namespace(deployment)
            and result.get("machine_deployment_name")
            == deployment.spec.get("worker_provisioning", {}).get(
                "machine_deployment_name"
            )
            and result.get("cluster_uid") == expected.get("expected_cluster_uid")
            and result.get("provisioning_plan_hash")
            == expected.get("provisioning_plan_hash")
            and result.get("machine_deployment_uid") is None
            and isinstance(result.get("machine_deployment_manifest_hash"), str)
            and len(result.get("machine_deployment_manifest_hash")) == 64
            and result.get("execution_mode") == "server_side_dry_run_simulation"
            and result.get("simulation_status") == "ready_for_manual_execution"
            and result.get("dry_run_passed") is True
            and result.get("execution_allowed") is False
            and result.get("side_effects") is False
            and result.get("created") is False
            and result.get("worker_creation_performed") is False
            and result.get("idempotent_reconciliation") is False
            and result.get("network_profile_hash")
            == _spec_hash(
                deployment.spec.get("worker_provisioning", {}).get(
                    "network_profile", {}
                )
            )
            and result.get("worker_readiness_verified") is False
            and result.get("worker_machine_count") == 0
            and result.get("ready_worker_machine_count") == 0
            and result.get("not_ready_worker_machine_names") == []
            and result.get("worker_node_names") == []
        )
        if not valid_result:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "TARGET_WORKER_PROVISIONING_SIMULATION_FAILED"}
            deployment.finished_at = timezone.now()
            deployment.save(
                update_fields=["status", "error", "finished_at", "updated_at"]
            )
            return
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "worker_provisioning_simulation": result,
            "dual_agent": {
                **((deployment.plan_result or {}).get("dual_agent", {})),
                "phase": "worker_provisioning_simulated_manual_execution_required",
                "worker_provisioning_completed": False,
                "worker_provisioning_simulated": True,
                "eph_reclaim_allowed": False,
            },
        }
        deployment.status = ClusterDeployment.Status.PLAN_READY
        deployment.error = None
        deployment.save(update_fields=["plan_result", "status", "error", "updated_at"])
    elif command.command_type == "cluster.postflight":
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "postflight": result,
        }
        node_count = result.get("node_count")
        not_ready_nodes = result.get("not_ready_nodes")
        expected_worker_replicas = deployment.spec.get("worker_replicas")
        worker_machine_count = result.get("worker_machine_count")
        ready_worker_machine_count = result.get("ready_worker_machine_count")
        not_ready_worker_names = result.get("not_ready_worker_machine_names")
        worker_node_names = result.get("worker_node_names")
        valid_result = (
            command.node.node_type == "target_cluster"
            and command.node_id == deployment.target_node_id
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("namespace") == _deployment_capi_namespace(deployment)
            and result.get("expected_worker_replicas") == expected_worker_replicas
            and (
                not isinstance(deployment.spec.get("worker_provisioning"), dict)
                or result.get("machine_deployment_name")
                == deployment.spec.get("worker_provisioning", {}).get(
                    "machine_deployment_name"
                )
            )
            and result.get("side_effects") is False
            and result.get("ready") is True
            and isinstance(node_count, int)
            and not isinstance(node_count, bool)
            and node_count > 0
            and isinstance(not_ready_nodes, list)
            and not not_ready_nodes
            and isinstance(expected_worker_replicas, int)
            and not isinstance(expected_worker_replicas, bool)
            and expected_worker_replicas >= 0
            and isinstance(worker_machine_count, int)
            and not isinstance(worker_machine_count, bool)
            and worker_machine_count == expected_worker_replicas
            and isinstance(ready_worker_machine_count, int)
            and not isinstance(ready_worker_machine_count, bool)
            and ready_worker_machine_count == expected_worker_replicas
            and isinstance(not_ready_worker_names, list)
            and not not_ready_worker_names
            and isinstance(worker_node_names, list)
            and len(worker_node_names) == expected_worker_replicas
            and all(isinstance(name, str) and name for name in worker_node_names)
            and len(set(worker_node_names)) == expected_worker_replicas
            and result.get("capi_worker_inventory_complete") is True
        )
        if valid_result:
            _advance_deployment_checkpoint(
                deployment,
                deployment.target_node,
                "completed",
                "postflight",
                None,
            )
            deployment.status = ClusterDeployment.Status.SUCCEEDED
            deployment.error = None
        else:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {
                "code": "TARGET_CLUSTER_POSTFLIGHT_FAILED",
                "not_ready_nodes": (
                    [str(item)[:120] for item in not_ready_nodes[:100]]
                    if isinstance(not_ready_nodes, list)
                    else []
                ),
                "not_ready_worker_machine_names": (
                    [str(item)[:120] for item in not_ready_worker_names[:100]]
                    if isinstance(not_ready_worker_names, list)
                    else []
                ),
                "worker_machine_count": worker_machine_count,
                "expected_worker_replicas": expected_worker_replicas,
            }
        deployment.finished_at = timezone.now()
        deployment.save(
            update_fields=[
                "plan_result",
                "status",
                "error",
                "finished_at",
                "started_at",
                "updated_at",
            ],
        )
    elif command.command_type == "cluster.plan":
        plan_hash = result.get("plan_hash")
        missing_requirements = result.get("missing_requirements")
        plan_valid = (
            command.node.node_type == "eph_node"
            and result.get("plan_status") == "validated_dry_run"
            and result.get("cluster_name") == deployment.cluster_name
            and result.get("kubernetes_version") == deployment.kubernetes_version
            and result.get("control_plane_replicas")
            == deployment.spec.get("control_plane_replicas")
            and result.get("worker_replicas") == deployment.spec.get("worker_replicas")
            and isinstance(plan_hash, str)
            and plan_hash == _spec_hash(deployment.spec)
            and isinstance(missing_requirements, list)
            and len(missing_requirements) <= 100
            and all(isinstance(item, str) and len(item) <= 200 for item in missing_requirements)
            and isinstance(result.get("provisioning_ready"), bool)
            and isinstance(result.get("adapter_enabled"), bool)
            and isinstance(result.get("side_effects"), bool)
        )
        if not plan_valid or (
            result.get("provisioning_ready") is True
            and result.get("adapter_enabled") is not True
        ):
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {"code": "INVALID_OR_INCONSISTENT_CLUSTER_PLAN"}
            deployment.finished_at = timezone.now()
            deployment.save(
                update_fields=[
                    "status",
                    "error",
                    "started_at",
                    "finished_at",
                    "updated_at",
                ],
            )
            return
        deployment.plan_result = result
        deployment.plan_hash = plan_hash
        deployment.status = ClusterDeployment.Status.PLAN_READY
        deployment.save(
            update_fields=[
                "plan_result",
                "plan_hash",
                "status",
                "started_at",
                "updated_at",
            ],
        )
