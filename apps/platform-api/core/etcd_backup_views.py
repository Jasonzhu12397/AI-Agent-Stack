"""The read-only endpoint an operator opens to see a cluster's backups."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.cluster_deployment_views import _can_edit_project
from core.etcd_backup_state import POLICY_FIELDS, etcd_backup_state, store_policy
from core.models import ClusterDeployment, ProjectMembership


def _deployment_or_error(request, deployment_id):
    deployment = get_object_or_404(
        ClusterDeployment.objects.filter(tenant__memberships__user=request.user),
        pk=deployment_id,
    )
    if not _can_edit_project(request.user, deployment.tenant, deployment.project):
        if not ProjectMembership.objects.filter(
            project=deployment.project, user=request.user
        ).exists():
            return None, Response({"detail": "Project membership required."}, status=403)
    return deployment, None


class ClusterDeploymentEtcdBackupView(APIView):
    """What backups exist for this cluster, and how old the newest one is."""

    permission_classes = [IsAuthenticated]

    def get(self, request, deployment_id):
        deployment, error = _deployment_or_error(request, deployment_id)
        if error is not None:
            return error
        return Response(
            {
                "deployment_id": str(deployment.id),
                "cluster_name": deployment.cluster_name,
                **etcd_backup_state(deployment),
            }
        )


class ClusterDeploymentEtcdBackupPolicyView(APIView):
    """Set the retention policy this cluster declares, checked by the contract."""

    permission_classes = [IsAuthenticated]

    def post(self, request, deployment_id):
        deployment, error = _deployment_or_error(request, deployment_id)
        if error is not None:
            return error
        if not policy_keys_only(request.data):
            return Response(
                {"detail": "Only policy fields may be set.", "code": "policy_fields_only"},
                status=400,
            )
        values = {
            field: request.data[field]
            for field in POLICY_FIELDS
            if field in request.data
        }
        if "enabled" in request.data:
            values["enabled"] = request.data["enabled"]
        if not values:
            return Response(
                {"detail": "No policy fields were provided.", "code": "policy_empty"},
                status=400,
            )
        try:
            store_policy(deployment, values)
        except ValueError as exc:
            return Response(
                {
                    "detail": getattr(exc, "detail", str(exc)),
                    "code": getattr(exc, "code", "policy_invalid"),
                },
                status=400,
            )
        record_audit_event(
            request,
            deployment.tenant,
            "cluster.etcd_backup.policy.updated",
            "cluster_deployment",
            deployment.id,
            project=deployment.project,
            metadata={"cluster_name": deployment.cluster_name, "policy": values},
        )
        return Response(
            {
                "deployment_id": str(deployment.id),
                "cluster_name": deployment.cluster_name,
                **etcd_backup_state(deployment),
            }
        )


def policy_keys_only(data) -> bool:
    """True when every key in the body is a policy field this endpoint owns."""

    allowed = set(POLICY_FIELDS) | {"enabled"}
    return set(data or {}) <= allowed
