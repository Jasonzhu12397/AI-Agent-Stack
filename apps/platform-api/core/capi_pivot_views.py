import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from core.agent_node_views import _authenticate_node
from core.models import (
    AgentNode,
    ClusterDeployment,
    ClusterDeploymentPivotEvent,
    ClusterDeploymentPivotOperation,
)


def _digest(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fresh_node(request, node_id):
    node = _authenticate_node(request, node_id)
    if (
        node is None
        or node.status != AgentNode.Status.ONLINE
        or node.last_seen_at is None
        or node.last_seen_at < timezone.now() - timezone.timedelta(seconds=60)
    ):
        return None
    return node


def _append_event(operation, actor, event_type, next_state, evidence):
    sequence = operation.version + 1
    ClusterDeploymentPivotEvent.objects.create(
        operation=operation,
        sequence=sequence,
        actor_node=actor,
        event_type=event_type,
        from_state=operation.state,
        to_state=next_state,
        evidence=evidence,
    )
    operation.state = next_state
    operation.version = sequence
    operation.journal = {
        **(operation.journal if isinstance(operation.journal, dict) else {}),
        "last_event": event_type,
        "last_event_sequence": sequence,
        "last_event_at": timezone.now().isoformat(),
    }
    operation.save(
        update_fields=["state", "version", "journal", "updated_at"],
    )


class AgentRuntimeCapiPivotOperationView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, deployment_id):
        node = _fresh_node(request, request.query_params.get("node_id"))
        if node is None:
            return Response({"detail": "Invalid or stale agent credential."}, status=401)
        operation = ClusterDeploymentPivotOperation.objects.filter(
            deployment_id=deployment_id,
            source_node=node,
        ).first()
        if operation is None:
            operation = ClusterDeploymentPivotOperation.objects.filter(
                deployment_id=deployment_id,
                target_node=node,
            ).first()
        if operation is None:
            return Response({"detail": "Pivot operation not assigned to this agent."}, status=404)
        return Response(self._serialize(operation))

    def post(self, request, deployment_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node = _fresh_node(request, request.data.get("node_id"))
        if node is None:
            return Response({"detail": "Invalid or stale agent credential."}, status=401)
        action = request.data.get("action")
        evidence = request.data.get("evidence", {})
        if not isinstance(evidence, dict):
            return Response({"detail": "evidence must be a JSON object."}, status=400)
        if len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")) > 65536:
            return Response({"detail": "evidence exceeds 64 KiB."}, status=413)

        with transaction.atomic():
            deployment = ClusterDeployment.objects.select_for_update().filter(
                pk=deployment_id,
                tenant=node.tenant,
                project=node.project,
            ).first()
            if deployment is None:
                return Response({"detail": "Deployment not found."}, status=404)
            operation = ClusterDeploymentPivotOperation.objects.select_for_update().filter(
                deployment=deployment,
            ).first()

            if action == "prepare":
                if node.node_type != AgentNode.NodeType.EPH_NODE or node.id != deployment.eph_node_id:
                    return Response({"detail": "Only the assigned EPH Agent can prepare a pivot."}, status=403)
                if operation is not None:
                    return Response(self._serialize(operation), status=200)
                source_identity = evidence.get("source_identity")
                inventory = evidence.get("inventory")
                inventory_sha256 = evidence.get("inventory_sha256")
                operation_id = evidence.get("operation_id")
                if (
                    not isinstance(source_identity, dict)
                    or not isinstance(source_identity.get("uid"), str)
                    or not source_identity["uid"]
                    or not isinstance(inventory, list)
                    or not inventory
                    or not isinstance(inventory_sha256, str)
                    or len(inventory_sha256) != 64
                    or not isinstance(operation_id, str)
                ):
                    return Response({"detail": "Source identity and complete inventory are required."}, status=400)
                if _digest(inventory) != inventory_sha256:
                    return Response({"detail": "Inventory digest does not match submitted inventory."}, status=409)
                if deployment.target_node_id is None:
                    return Response({"detail": "A target agent must be assigned before pivot."}, status=409)
                try:
                    operation_uuid = uuid.UUID(operation_id)
                except (TypeError, ValueError, AttributeError):
                    return Response({"detail": "operation_id must be a UUID."}, status=400)
                operation = ClusterDeploymentPivotOperation.objects.create(
                    deployment=deployment,
                    operation_id=operation_uuid,
                    source_node=deployment.eph_node,
                    target_node=deployment.target_node,
                    source_identity=source_identity,
                    inventory=inventory,
                    inventory_sha256=inventory_sha256,
                    state=ClusterDeploymentPivotOperation.State.WAITING_TARGET_ACK,
                )
                _append_event(
                    operation,
                    node,
                    "source_inventory_prepared",
                    ClusterDeploymentPivotOperation.State.WAITING_TARGET_ACK,
                    {"inventory_sha256": inventory_sha256, "inventory_count": len(inventory)},
                )
                return Response(self._serialize(operation), status=201)

            if operation is None:
                return Response({"detail": "Pivot operation has not been prepared."}, status=404)
            if node.id not in {operation.source_node_id, operation.target_node_id}:
                return Response({"detail": "Agent is not a participant in this pivot."}, status=403)

            if action == "target_ack":
                if node.id != operation.target_node_id or node.node_type != AgentNode.NodeType.TARGET_CLUSTER:
                    return Response({"detail": "Only the assigned Target Agent may acknowledge."}, status=403)
                if operation.state != ClusterDeploymentPivotOperation.State.WAITING_TARGET_ACK:
                    return Response({"detail": "Pivot is not waiting for target acknowledgement."}, status=409)
                if evidence.get("operation_id") != str(operation.operation_id):
                    return Response({"detail": "Target acknowledgement operation_id mismatch."}, status=409)
                if evidence.get("inventory_sha256") != operation.inventory_sha256:
                    return Response({"detail": "Target acknowledgement inventory digest mismatch."}, status=409)
                identity = evidence.get("target_identity")
                if (
                    not isinstance(identity, dict)
                    or not isinstance(identity.get("cluster_uid"), str)
                    or not identity["cluster_uid"]
                    or not isinstance(identity.get("api_version"), str)
                    or not isinstance(identity.get("spec_sha256"), str)
                ):
                    return Response({"detail": "Target acknowledgement lacks verified cluster identity."}, status=400)
                operation.target_ack = evidence
                operation.target_identity = identity
                operation.approval_hash = evidence.get("approval_hash", "")
                _append_event(
                    operation,
                    node,
                    "target_agent_acknowledged",
                    ClusterDeploymentPivotOperation.State.READY,
                    {"inventory_sha256": operation.inventory_sha256, "target_cluster_uid": identity["cluster_uid"]},
                )
                return Response(self._serialize(operation))

            if action == "begin":
                if node.id != operation.source_node_id or node.node_type != AgentNode.NodeType.EPH_NODE:
                    return Response({"detail": "Only the source EPH Agent may begin clusterctl move."}, status=403)
                if operation.state != ClusterDeploymentPivotOperation.State.READY:
                    return Response({"detail": "Target acknowledgement is not complete."}, status=409)
                approval_hash = evidence.get("approval_hash")
                if not isinstance(approval_hash, str) or not approval_hash or approval_hash != operation.approval_hash:
                    return Response({"detail": "Pivot approval hash is missing or mismatched."}, status=409)
                operation.started_at = timezone.now()
                operation.save(update_fields=["started_at", "updated_at"])
                _append_event(
                    operation,
                    node,
                    "clusterctl_move_started",
                    ClusterDeploymentPivotOperation.State.EXECUTING,
                    {"approval_hash": approval_hash},
                )
                return Response(self._serialize(operation))

            if action == "mark_verifying":
                if node.id != operation.source_node_id or operation.state != ClusterDeploymentPivotOperation.State.EXECUTING:
                    return Response({"detail": "Invalid source verification transition."}, status=409)
                _append_event(operation, node, "clusterctl_move_returned", ClusterDeploymentPivotOperation.State.VERIFYING, {})
                return Response(self._serialize(operation))

            if action == "mark_uncertain":
                if node.id != operation.source_node_id or operation.state not in {
                    ClusterDeploymentPivotOperation.State.EXECUTING,
                    ClusterDeploymentPivotOperation.State.VERIFYING,
                }:
                    return Response({"detail": "Invalid uncertain transition."}, status=409)
                operation.last_error = evidence
                _append_event(operation, node, "execution_outcome_uncertain", ClusterDeploymentPivotOperation.State.UNCERTAIN, evidence)
                return Response(self._serialize(operation))

            if action == "target_verified":
                if node.id != operation.target_node_id or operation.state not in {
                    ClusterDeploymentPivotOperation.State.VERIFYING,
                    ClusterDeploymentPivotOperation.State.UNCERTAIN,
                }:
                    return Response({"detail": "Target verification is not allowed in this state."}, status=409)
                if evidence.get("inventory_sha256") != operation.inventory_sha256:
                    return Response({"detail": "Verified inventory digest mismatch."}, status=409)
                objects = evidence.get("objects")
                if not isinstance(objects, list) or len(objects) != len(operation.inventory):
                    return Response({"detail": "Target object inventory is incomplete."}, status=409)
                if any(not isinstance(item, dict) or not isinstance(item.get("uid"), str) or not item["uid"] for item in objects):
                    return Response({"detail": "Every adopted target object must have an API-server UID."}, status=409)
                operation.target_identity = {
                    **(operation.target_identity or {}),
                    "verified_objects": objects,
                    "verified_at": timezone.now().isoformat(),
                }
                _append_event(operation, node, "target_inventory_verified", ClusterDeploymentPivotOperation.State.VERIFYING, {"object_count": len(objects)})
                return Response(self._serialize(operation))

            if action == "source_absent":
                if node.id != operation.source_node_id or operation.state != ClusterDeploymentPivotOperation.State.VERIFYING:
                    return Response({"detail": "Source absence verification is not allowed."}, status=409)
                if not isinstance(operation.target_identity, dict) or not isinstance(operation.target_identity.get("verified_objects"), list):
                    return Response({"detail": "Target inventory must be independently verified first."}, status=409)
                if evidence.get("source_cluster_absent") is not True or evidence.get("remaining_source_objects") != 0:
                    return Response({"detail": "Source ownership is not proven absent."}, status=409)
                operation.completed_at = timezone.now()
                _append_event(operation, node, "source_inventory_absent", ClusterDeploymentPivotOperation.State.COMPLETED, evidence)
                return Response(self._serialize(operation))

            return Response({"detail": "Unsupported pivot operation action."}, status=400)

    @staticmethod
    def _serialize(operation):
        return {
            "deployment_id": str(operation.deployment_id),
            "operation_id": str(operation.operation_id),
            "state": operation.state,
            "version": operation.version,
            "inventory_sha256": operation.inventory_sha256,
            "source_identity": operation.source_identity,
            "target_ack": operation.target_ack,
            "target_identity": operation.target_identity,
            "last_error": operation.last_error,
            "started_at": operation.started_at,
            "completed_at": operation.completed_at,
            "eph_reclaim_allowed": operation.state == ClusterDeploymentPivotOperation.State.COMPLETED,
        }
