import re

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.agent_node_views import _payload_hash, _tenant_and_project, _validate_payload
from core.cluster_deployment_services import queue_deployment_command
from core.models import (
    AgentCommand,
    AgentNode,
    ClusterDeployment,
    ProjectMembership,
    TenantMembership,
)


def _deployment_data(deployment):
    return {
        "id": str(deployment.id),
        "tenant_id": str(deployment.tenant_id),
        "project_id": str(deployment.project_id),
        "cluster_name": deployment.cluster_name,
        "kubernetes_version": deployment.kubernetes_version,
        "eph_node_id": str(deployment.eph_node_id),
        "target_node_id": (
            str(deployment.target_node_id) if deployment.target_node_id else None
        ),
        "status": deployment.status,
        "spec": deployment.spec,
        "plan_hash": deployment.plan_hash,
        "preflight_result": deployment.preflight_result,
        "plan_result": deployment.plan_result,
        "error": deployment.error,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
        "finished_at": deployment.finished_at,
    }


def _can_edit_project(user, tenant, project):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_editor = ProjectMembership.objects.filter(
        project=project,
        user=user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()
    return tenant_admin or project_editor


class ClusterDeploymentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        deployments = ClusterDeployment.objects.filter(
            tenant=tenant,
            project=project,
        )[:50]
        return Response(
            {
                "count": len(deployments),
                "results": [_deployment_data(item) for item in deployments],
            }
        )

    def post(self, request, tenant_id, project_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        if not _can_edit_project(request.user, tenant, project):
            return Response({"detail": "Project editor permission required."}, status=403)
        eph_node_id = request.data.get("eph_node_id")
        eph_node = get_object_or_404(
            AgentNode.objects.filter(
                tenant=tenant,
                project=project,
                node_type=AgentNode.NodeType.EPH_NODE,
            ),
            pk=eph_node_id,
        )
        if eph_node.status != AgentNode.Status.ONLINE or (
            eph_node.last_seen_at is None
            or eph_node.last_seen_at < timezone.now() - timezone.timedelta(seconds=60)
        ):
            return Response(
                {"detail": "The selected EPH agent must have a fresh heartbeat."},
                status=409,
            )
        spec = request.data.get("cluster_spec")
        payload_error = _validate_payload({"cluster_spec": spec})
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        cluster_name = spec.get("cluster_name")
        kubernetes_version = spec.get("kubernetes_version")
        control_plane_replicas = spec.get("control_plane_replicas")
        worker_replicas = spec.get("worker_replicas")
        if (
            not isinstance(cluster_name, str)
            or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
            or len(cluster_name) > 63
        ):
            return Response({"detail": "cluster_name must be a DNS label."}, status=400)
        if (
            not isinstance(kubernetes_version, str)
            or not re.fullmatch(r"v1\.\d+\.\d+", kubernetes_version)
        ):
            return Response({"detail": "kubernetes_version must use v1.x.y format."}, status=400)
        if (
            not isinstance(control_plane_replicas, int)
            or isinstance(control_plane_replicas, bool)
            or control_plane_replicas < 1
            or control_plane_replicas > 9
            or control_plane_replicas % 2 == 0
        ):
            return Response(
                {"detail": "control_plane_replicas must be an odd integer from 1 to 9."},
                status=400,
            )
        if (
            not isinstance(worker_replicas, int)
            or isinstance(worker_replicas, bool)
            or worker_replicas < 0
            or worker_replicas > 1000
        ):
            return Response(
                {"detail": "worker_replicas must be an integer from 0 to 1000."},
                status=400,
            )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)
        try:
            with transaction.atomic():
                deployment, created = ClusterDeployment.objects.get_or_create(
                    tenant=tenant,
                    idempotency_key=idempotency_key,
                    defaults={
                        "project": project,
                        "eph_node": eph_node,
                        "requested_by": request.user,
                        "cluster_name": cluster_name,
                        "kubernetes_version": kubernetes_version,
                        "spec": spec,
                        "status": ClusterDeployment.Status.PREFLIGHT_QUEUED,
                    },
                )
                if created:
                    queue_deployment_command(deployment, "host.preflight")
        except IntegrityError:
            deployment = ClusterDeployment.objects.get(
                tenant=tenant,
                idempotency_key=idempotency_key,
            )
            created = False
        if not created and (
            deployment.project_id != project.id
            or deployment.eph_node_id != eph_node.id
            or deployment.spec != spec
        ):
            return Response(
                {"detail": "Idempotency-Key was used for a different cluster plan."},
                status=409,
            )
        return Response(
            _deployment_data(deployment),
            status=202 if created else 200,
        )


class ClusterDeploymentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, deployment_id):
        deployment = get_object_or_404(
            ClusterDeployment.objects.filter(tenant__memberships__user=request.user),
            pk=deployment_id,
        )
        if not _can_edit_project(request.user, deployment.tenant, deployment.project):
            if not ProjectMembership.objects.filter(
                project=deployment.project,
                user=request.user,
            ).exists():
                return Response({"detail": "Project membership required."}, status=403)
        return Response(_deployment_data(deployment))


class ClusterDeploymentCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, deployment_id):
        deployment = get_object_or_404(
            ClusterDeployment.objects.filter(tenant__memberships__user=request.user),
            pk=deployment_id,
        )
        if not _can_edit_project(request.user, deployment.tenant, deployment.project):
            return Response({"detail": "Project editor permission required."}, status=403)
        with transaction.atomic():
            deployment = ClusterDeployment.objects.select_for_update().get(pk=deployment.pk)
            if deployment.status in {
                ClusterDeployment.Status.SUCCEEDED,
                ClusterDeployment.Status.FAILED,
                ClusterDeployment.Status.CANCELLED,
            }:
                return Response({"detail": "Deployment is already terminal."}, status=409)
            deployment.status = ClusterDeployment.Status.CANCELLED
            deployment.finished_at = timezone.now()
            deployment.save(update_fields=["status", "finished_at", "updated_at"])
            AgentCommand.objects.filter(
                deployment=deployment,
                status__in=[
                    AgentCommand.Status.QUEUED,
                    AgentCommand.Status.WAITING_APPROVAL,
                    AgentCommand.Status.RUNNING,
                ],
            ).update(
                status=AgentCommand.Status.CANCELLED,
                finished_at=timezone.now(),
                lease_token=None,
                lease_expires_at=None,
            )
        return Response(_deployment_data(deployment))
