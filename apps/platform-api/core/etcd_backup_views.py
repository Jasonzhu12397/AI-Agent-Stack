"""The read-only endpoint an operator opens to see a cluster's backups."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.cluster_deployment_views import _can_edit_project
from core.etcd_backup_state import etcd_backup_state
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
