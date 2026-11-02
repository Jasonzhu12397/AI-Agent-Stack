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


def _spec_hash(spec: dict) -> str:
    encoded = json.dumps(
        spec,
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
    payload = {"deployment_id": str(deployment.id)}
    if command_type == "cluster.postflight":
        payload["cluster_name"] = deployment.cluster_name
    elif command_type == "cluster.capi.pivot.target.preflight":
        payload["cluster_name"] = deployment.cluster_name
        payload["namespace"] = deployment.spec.get("capi_namespace", "default")
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
    elif command.command_type == "cluster.capi.pivot.target.preflight":
        controllers = result.get("controllers")
        missing_resources = result.get("missing_resources")
        conflicts = result.get("cluster_name_conflicts")
        blockers = result.get("blockers")
        expected_namespace = command.payload.get("namespace", "default")
        valid_controllers = (
            isinstance(controllers, list)
            and len(controllers) == 4
            and all(
                isinstance(item, dict)
                and isinstance(item.get("namespace"), str)
                and isinstance(item.get("deployment"), str)
                and isinstance(item.get("ready"), bool)
                for item in controllers
            )
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
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "capi_pivot_preflight": {
                **result,
                "preflight_validated": ready,
            },
        }
        deployment.error = None
        deployment.save(
            update_fields=["plan_result", "error", "started_at", "updated_at"],
        )
    elif command.command_type == "cluster.postflight":
        deployment.plan_result = {
            **(deployment.plan_result or {}),
            "postflight": result,
        }
        node_count = result.get("node_count")
        not_ready_nodes = result.get("not_ready_nodes")
        valid_result = (
            command.node.node_type == "target_cluster"
            and result.get("ready") is True
            and isinstance(node_count, int)
            and not isinstance(node_count, bool)
            and node_count > 0
            and isinstance(not_ready_nodes, list)
            and not not_ready_nodes
        )
        if valid_result:
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
