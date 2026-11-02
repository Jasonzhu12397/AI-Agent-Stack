import hashlib
import json

from django.utils import timezone

from core.models import AgentCommand, ClusterDeployment


def _payload_hash(command_type: str, payload: dict) -> str:
    encoded = json.dumps(
        {"command_type": command_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def queue_deployment_command(
    deployment: ClusterDeployment,
    command_type: str,
    node=None,
) -> AgentCommand:
    payload = {
        "deployment_id": str(deployment.id),
        "cluster_spec": deployment.spec,
    }
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
            "status": AgentCommand.Status.QUEUED,
            "plan_hash": _payload_hash(command_type, payload),
        },
    )
    return command


def handle_deployment_command_result(
    command: AgentCommand,
    outcome: str,
    result: dict,
    error: dict | None,
) -> None:
    if command.deployment_id is None:
        return
    deployment = ClusterDeployment.objects.select_for_update().get(
        pk=command.deployment_id,
    )
    if deployment.status in {
        ClusterDeployment.Status.CANCELLED,
        ClusterDeployment.Status.SUCCEEDED,
        ClusterDeployment.Status.FAILED,
    }:
        return
    if deployment.started_at is None:
        deployment.started_at = timezone.now()
    if outcome == "failed":
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
        deployment.preflight_result = result
        deployment.status = ClusterDeployment.Status.PLANNING
        deployment.save(
            update_fields=["preflight_result", "status", "started_at", "updated_at"],
        )
        queue_deployment_command(deployment, "cluster.plan")
    elif command.command_type == "cluster.postflight":
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "postflight": result,
        }
        if result.get("ready") is True and result.get("node_count", 0) > 0:
            deployment.status = ClusterDeployment.Status.SUCCEEDED
            deployment.error = None
        else:
            deployment.status = ClusterDeployment.Status.FAILED
            deployment.error = {
                "code": "TARGET_CLUSTER_POSTFLIGHT_FAILED",
                "not_ready_nodes": result.get("not_ready_nodes", []),
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
        deployment.plan_result = result
        deployment.plan_hash = str(result.get("plan_hash", command.plan_hash))[:64]
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
