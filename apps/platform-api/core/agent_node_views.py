# ruff: noqa: I001
import datetime
import hashlib
import hmac
import json
import secrets
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.cluster_deployment_services import handle_deployment_command_result
from core.models import (
    AgentCommand,
    AgentNode,
    ClusterDeployment,
    ClusterDeploymentCheckpoint,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)


EPH_COMMANDS = {
    "node.health",
    "host.preflight",
    "cluster.plan",
    "cluster.provision",
    "target-agent.bootstrap",
    "capi.cluster.observe",
    "metal3.host.inspect",
    "cluster.provider.discover",
    "cluster.manifest.validate",
    "cluster.capi.pivot.source.preflight",
}
TARGET_COMMANDS = {
    "node.health",
    "cluster.health",
    "cluster.observe",
    "cluster.diagnose",
    "cluster.postflight",
    "cluster.control-plane.verify",
    "cluster.capi.pivot.target.preflight",
    "k8s.resource.read",
}
MUTATING_COMMANDS = {"cluster.provision", "target-agent.bootstrap"}
FORBIDDEN_PAYLOAD_KEYS = {
    "shell",
    "command",
    "script",
    "raw_manifest",
    "manifest",
    "kubeconfig",
    "password",
    "secret",
    "token",
    "private_key",
}


def _tenant_and_project(request, tenant_id, project_id):
    tenant = get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )
    is_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    projects = Project.objects.filter(tenant=tenant)
    if not is_admin:
        projects = projects.filter(memberships__user=request.user)
    project = get_object_or_404(projects.distinct(), pk=project_id)
    return tenant, project, is_admin


def _node_data(node):
    return {
        "id": str(node.id),
        "tenant_id": str(node.tenant_id),
        "project_id": str(node.project_id),
        "name": node.name,
        "node_type": node.node_type,
        "cluster_name": node.cluster_name,
        "agent_version": node.agent_version,
        "capabilities": node.capabilities,
        "labels": node.labels,
        "status": node.status,
        "last_seen_at": node.last_seen_at,
        "last_seen_ip": node.last_seen_ip,
        "created_at": node.created_at,
    }


def _command_data(command):
    return {
        "id": str(command.id),
        "node_id": str(command.node_id),
        "deployment_id": (
            str(command.deployment_id) if command.deployment_id else None
        ),
        "command_type": command.command_type,
        "payload": command.payload,
        "status": command.status,
        "plan_hash": command.plan_hash,
        "approved_at": command.approved_at,
        "attempts": command.attempts,
        "result": command.result,
        "error": command.error,
        "created_at": command.created_at,
        "started_at": command.started_at,
        "finished_at": command.finished_at,
    }


def _payload_hash(command_type, payload):
    encoded = json.dumps(
        {"command_type": command_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_size_bytes(value):
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _validate_payload(payload):
    if not isinstance(payload, dict):
        return "payload must be a JSON object."
    if _json_size_bytes(payload) > 65536:
        return "payload must not exceed 64 KiB."

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                    return False
                if not visit(child):
                    return False
        elif isinstance(value, list):
            return all(visit(child) for child in value)
        return True

    if not visit(payload):
        return "Payload contains a forbidden executable or credential field."
    return None


def _authenticate_node(request, node_id):
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return None
    try:
        node_id = uuid.UUID(str(node_id))
    except (TypeError, ValueError, AttributeError):
        return None
    node = AgentNode.objects.filter(pk=node_id).first()
    if node is None or node.status == AgentNode.Status.REVOKED:
        return None
    supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_hash, node.credential_hash):
        return None
    return node


class AgentNodeListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        stale_before = timezone.now() - datetime.timedelta(seconds=60)
        AgentNode.objects.filter(
            tenant=tenant,
            project=project,
            status=AgentNode.Status.ONLINE,
            last_seen_at__lt=stale_before,
        ).update(status=AgentNode.Status.OFFLINE)
        nodes = AgentNode.objects.filter(tenant=tenant, project=project)
        return Response({"count": nodes.count(), "results": [_node_data(node) for node in nodes]})

    def post(self, request, tenant_id, project_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, is_admin = _tenant_and_project(request, tenant_id, project_id)
        if not is_admin:
            return Response(
                {"detail": "Tenant owner or admin role is required to enroll node agents."},
                status=status.HTTP_403_FORBIDDEN,
            )
        name = str(request.data.get("name", "")).strip()
        node_type = request.data.get("node_type")
        cluster_name = str(request.data.get("cluster_name", "")).strip()
        if not name or len(name) > 120:
            return Response(
                {"detail": "name is required and must be at most 120 characters."},
                status=400,
            )
        if node_type not in AgentNode.NodeType.values:
            return Response({"detail": "node_type must be eph_node or target_cluster."}, status=400)
        capabilities = request.data.get("capabilities", [])
        labels = request.data.get("labels", {})
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            return Response({"detail": "capabilities must be a list of strings."}, status=400)
        if not isinstance(labels, dict):
            return Response({"detail": "labels must be a JSON object."}, status=400)
        raw_token = secrets.token_urlsafe(36)
        try:
            node = AgentNode.objects.create(
                tenant=tenant,
                project=project,
                created_by=request.user,
                name=name,
                node_type=node_type,
                cluster_name=cluster_name,
                capabilities=capabilities,
                labels=labels,
                credential_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            )
        except IntegrityError:
            return Response(
                {"detail": "A node with this name already exists in the project."},
                status=409,
            )
        record_audit_event(
            request,
            tenant,
            "agent.node.enrolled",
            "agent_node",
            node.id,
            project=project,
            metadata={"node_type": node.node_type, "node_name": node.name},
        )
        data = _node_data(node)
        data["enrollment_token"] = raw_token
        data["token_warning"] = (
            "Store this token securely; it is shown only once. "
            "Use TLS for agent communication."
        )
        return Response(data, status=status.HTTP_201_CREATED)


class AgentNodeRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, node_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node = get_object_or_404(
            AgentNode.objects.filter(tenant__memberships__user=request.user),
            pk=node_id,
        )
        is_admin = TenantMembership.objects.filter(
            tenant=node.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not is_admin:
            return Response({"detail": "Tenant owner or admin role is required."}, status=403)
        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if node.status == AgentNode.Status.REVOKED:
                return Response({"detail": "Agent node has been revoked."}, status=409)
            node.status = AgentNode.Status.REVOKED
            node.revoked_at = timezone.now()
            node.save(update_fields=["status", "revoked_at", "updated_at"])
            AgentCommand.objects.filter(
                node=node,
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
        record_audit_event(
            request,
            node.tenant,
            "agent.node.revoked",
            "agent_node",
            node.id,
            project=node.project,
            metadata={"node_type": node.node_type, "node_name": node.name},
        )
        return Response(_node_data(node))


class AgentNodeCommandListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, node_id):
        node = get_object_or_404(
            AgentNode.objects.filter(tenant__memberships__user=request.user),
            pk=node_id,
        )
        project_member = ProjectMembership.objects.filter(
            project=node.project,
            user=request.user,
        ).exists()
        tenant_admin = TenantMembership.objects.filter(
            tenant=node.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not project_member and not tenant_admin:
            return Response({"detail": "Project membership required."}, status=403)
        commands = AgentCommand.objects.filter(node=node)[:100]
        return Response(
            {
                "count": len(commands),
                "results": [_command_data(item) for item in commands],
            }
        )

    def post(self, request, node_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node = get_object_or_404(
            AgentNode.objects.filter(tenant__memberships__user=request.user),
            pk=node_id,
        )
        if node.status == AgentNode.Status.REVOKED:
            return Response({"detail": "Agent node has been revoked."}, status=409)
        is_admin = TenantMembership.objects.filter(
            tenant=node.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        project_editor = ProjectMembership.objects.filter(
            project=node.project,
            user=request.user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
        if not (is_admin or project_editor):
            return Response({"detail": "Project editor permission required."}, status=403)
        command_type = request.data.get("command_type")
        allowed = EPH_COMMANDS if node.node_type == AgentNode.NodeType.EPH_NODE else TARGET_COMMANDS
        if not isinstance(command_type, str) or command_type not in allowed:
            return Response({"detail": "Command is not allowed for this agent type."}, status=400)
        payload = request.data.get("payload", {})
        payload_error = _validate_payload(payload)
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)
        plan_hash = _payload_hash(command_type, payload)
        initial_status = (
            AgentCommand.Status.WAITING_APPROVAL
            if command_type in MUTATING_COMMANDS
            else AgentCommand.Status.QUEUED
        )
        try:
            with transaction.atomic():
                command, created = AgentCommand.objects.get_or_create(
                    tenant=node.tenant,
                    idempotency_key=idempotency_key,
                    defaults={
                        "project": node.project,
                        "node": node,
                        "requested_by": request.user,
                        "command_type": command_type,
                        "payload": payload,
                        "status": initial_status,
                        "plan_hash": plan_hash,
                    },
                )
        except IntegrityError:
            command = AgentCommand.objects.get(
                tenant=node.tenant,
                idempotency_key=idempotency_key,
            )
            created = False
        if not created and (
            command.node_id != node.id
            or command.command_type != command_type
            or command.payload != payload
        ):
            return Response(
                {"detail": "Idempotency-Key was used for a different command."},
                status=409,
            )
        if created:
            event_type = (
                "agent.command.approval_requested"
                if initial_status == AgentCommand.Status.WAITING_APPROVAL
                else "agent.command.queued"
            )
            record_audit_event(
                request,
                node.tenant,
                event_type,
                "agent_command",
                command.id,
                project=node.project,
                metadata={
                    "command_type": command.command_type,
                    "status": command.status,
                    "plan_hash": command.plan_hash,
                },
            )
        return Response(
            _command_data(command),
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class AgentCommandApproveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, command_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        command = get_object_or_404(
            AgentCommand.objects.filter(tenant__memberships__user=request.user),
            pk=command_id,
        )
        is_admin = TenantMembership.objects.filter(
            tenant=command.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not is_admin:
            return Response(
                {"detail": "Tenant owner or admin role is required to approve provisioning."},
                status=403,
            )
        if command.requested_by_id == request.user.id:
            return Response(
                {"detail": "A different administrator must approve this command."},
                status=403,
            )
        if command.status != AgentCommand.Status.WAITING_APPROVAL:
            return Response({"detail": "Command is not waiting for approval."}, status=409)
        supplied_hash = str(request.data.get("plan_hash", ""))
        current_hash = _payload_hash(command.command_type, command.payload)
        if (
            not supplied_hash
            or not hmac.compare_digest(supplied_hash, command.plan_hash)
            or not hmac.compare_digest(current_hash, command.plan_hash)
        ):
            return Response(
                {"detail": "Approval plan hash does not match the immutable command payload."},
                status=409,
            )
        command.status = AgentCommand.Status.QUEUED
        command.approved_plan_hash = current_hash
        command.approved_by = request.user
        command.approved_at = timezone.now()
        command.save(
            update_fields=[
                "status",
                "approved_plan_hash",
                "approved_by",
                "approved_at",
                "updated_at",
            ]
        )
        record_audit_event(
            request,
            command.tenant,
            "agent.command.approved",
            "agent_command",
            command.id,
            project=command.project,
            metadata={
                "command_type": command.command_type,
                "plan_hash": command.approved_plan_hash,
            },
        )
        return Response(_command_data(command))


class AgentRuntimeHeartbeatView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node_id = request.data.get("node_id")
        node = _authenticate_node(request, node_id)
        if node is None:
            return Response({"detail": "Invalid or revoked node credential."}, status=401)
        capabilities = request.data.get("capabilities", node.capabilities)
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            return Response({"detail": "capabilities must be a list of strings."}, status=400)
        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if node.status == AgentNode.Status.REVOKED:
                return Response({"detail": "Invalid or revoked node credential."}, status=401)
            node.agent_version = str(request.data.get("agent_version", node.agent_version))[:40]
            node.capabilities = capabilities
            node.cluster_name = str(request.data.get("cluster_name", node.cluster_name))[:120]
            node.status = AgentNode.Status.ONLINE
            node.last_seen_at = timezone.now()
            node.last_seen_ip = request.META.get("REMOTE_ADDR")
            node.save(
                update_fields=[
                    "agent_version",
                    "capabilities",
                    "cluster_name",
                    "status",
                    "last_seen_at",
                    "last_seen_ip",
                    "updated_at",
                ]
            )
        active_deployments = []
        if node.node_type == AgentNode.NodeType.TARGET_CLUSTER:
            active_deployments = [
                {
                    "deployment_id": str(deployment.id),
                    "cluster_name": deployment.cluster_name,
                    "status": deployment.status,
                    "checkpoint_endpoint": (
                        f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/"
                    ),
                }
                for deployment in ClusterDeployment.objects.filter(
                    target_node=node,
                    status__in=[
                        ClusterDeployment.Status.CONTROL_PLANE_READY,
                        ClusterDeployment.Status.TARGET_AGENT_PENDING,
                        ClusterDeployment.Status.POSTFLIGHT,
                    ],
                ).order_by("created_at")
            ]
        return Response(
            {
                "node_id": str(node.id),
                "status": node.status,
                "server_time": node.last_seen_at,
                "active_deployments": active_deployments,
            }
        )


class AgentRuntimePollView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node_id = request.data.get("node_id")
        node = _authenticate_node(request, node_id)
        if node is None:
            return Response({"detail": "Invalid or revoked node credential."}, status=401)
        now = timezone.now()
        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if node.status == AgentNode.Status.REVOKED:
                return Response({"detail": "Invalid or revoked node credential."}, status=401)
            command = (
                AgentCommand.objects.select_for_update(skip_locked=True)
                .filter(node=node)
                .filter(
                    Q(status=AgentCommand.Status.QUEUED)
                    | Q(
                        status=AgentCommand.Status.RUNNING,
                        lease_expires_at__lt=now,
                    )
                )
                .order_by("created_at")
                .first()
            )
            if command is None:
                return Response({"command": None})
            allowed_commands = (
                EPH_COMMANDS
                if node.node_type == AgentNode.NodeType.EPH_NODE
                else TARGET_COMMANDS
            )
            if command.command_type not in allowed_commands:
                command.status = AgentCommand.Status.FAILED
                command.error = {
                    "code": "COMMAND_NOT_ALLOWED_FOR_AGENT_ROLE",
                    "retryable": False,
                }
                command.finished_at = now
                command.lease_token = None
                command.lease_expires_at = None
                command.save(
                    update_fields=[
                        "status",
                        "error",
                        "finished_at",
                        "lease_token",
                        "lease_expires_at",
                        "updated_at",
                    ],
                )
                return Response({"command": None})
            if command.attempts >= command.max_attempts:
                command.status = AgentCommand.Status.FAILED
                command.error = {"code": "LEASE_EXPIRED", "retryable": False}
                command.finished_at = now
                command.lease_token = None
                command.lease_expires_at = None
                command.save()
                return Response({"command": None})
            approval_expired = (
                command.approved_at is None
                or command.approved_at < now - datetime.timedelta(minutes=15)
            )
            approval_hash_invalid = (
                not command.approved_plan_hash
                or not hmac.compare_digest(
                    command.approved_plan_hash,
                    _payload_hash(command.command_type, command.payload),
                )
            )
            if command.command_type in MUTATING_COMMANDS and (
                approval_expired or approval_hash_invalid
            ):
                command.status = AgentCommand.Status.FAILED
                error_code = (
                    "APPROVAL_EXPIRED"
                    if approval_expired
                    else "APPROVAL_HASH_MISMATCH"
                )
                command.error = {"code": error_code, "retryable": False}
                command.finished_at = now
                command.save()
                return Response({"command": None})
            command.status = AgentCommand.Status.RUNNING
            command.attempts += 1
            command.lease_token = uuid.uuid4()
            command.lease_expires_at = now + datetime.timedelta(seconds=90)
            if command.started_at is None:
                command.started_at = now
            command.save(
                update_fields=[
                    "status",
                    "attempts",
                    "lease_token",
                    "lease_expires_at",
                    "started_at",
                    "updated_at",
                ]
            )
        return Response(
            {
                "command": {
                    "id": str(command.id),
                    "command_type": command.command_type,
                    "payload": command.payload,
                    "attempt": command.attempts,
                    "lease_token": str(command.lease_token),
                    "lease_expires_at": command.lease_expires_at,
                    "deployment_id": (
                        str(command.deployment_id) if command.deployment_id else None
                    ),
                }
            }
        )


class AgentRuntimeLeaseRenewView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, command_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node = _authenticate_node(request, request.data.get("node_id"))
        if node is None:
            return Response({"detail": "Invalid or revoked node credential."}, status=401)
        lease_token = request.headers.get("X-Agent-Lease", "")
        try:
            lease_token = uuid.UUID(lease_token)
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "A valid X-Agent-Lease header is required."}, status=400)
        now = timezone.now()
        with transaction.atomic():
            command = get_object_or_404(
                AgentCommand.objects.select_for_update(),
                pk=command_id,
                node=node,
                status=AgentCommand.Status.RUNNING,
                lease_token=lease_token,
                lease_expires_at__gte=now,
            )
            command.lease_expires_at = now + datetime.timedelta(seconds=90)
            command.save(update_fields=["lease_expires_at", "updated_at"])
        return Response(
            {
                "id": str(command.id),
                "lease_expires_at": command.lease_expires_at,
            }
        )


class AgentRuntimeResultView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, command_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node_id = request.data.get("node_id")
        node = _authenticate_node(request, node_id)
        if node is None:
            return Response({"detail": "Invalid or revoked node credential."}, status=401)
        lease_token = request.headers.get("X-Agent-Lease", "")
        try:
            lease_token = uuid.UUID(lease_token)
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "A valid X-Agent-Lease header is required."}, status=400)
        result = request.data.get("result", {})
        error = request.data.get("error")
        outcome = request.data.get("outcome")
        if outcome not in {"succeeded", "failed"}:
            return Response({"detail": "outcome must be succeeded or failed."}, status=400)
        if not isinstance(result, dict) or _json_size_bytes(result) > 65536:
            return Response(
                {"detail": "result must be a JSON object no larger than 64 KiB."},
                status=400,
            )
        if error is not None and not isinstance(error, dict):
            return Response({"detail": "error must be a JSON object."}, status=400)
        with transaction.atomic():
            command = get_object_or_404(
                AgentCommand.objects.select_for_update(),
                pk=command_id,
                node=node,
                status=AgentCommand.Status.RUNNING,
                lease_token=lease_token,
                lease_expires_at__gte=timezone.now(),
            )
            command.status = (
                AgentCommand.Status.SUCCEEDED
                if outcome == "succeeded"
                else AgentCommand.Status.FAILED
            )
            command.result = result if outcome == "succeeded" else None
            command.error = error if outcome == "failed" else None
            command.finished_at = timezone.now()
            command.lease_token = None
            command.lease_expires_at = None
            command.save(
                update_fields=[
                    "status",
                    "result",
                    "error",
                    "finished_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            handle_deployment_command_result(
                command,
                outcome,
                result,
                error,
            )
        return Response(
            {
                "id": str(command.id),
                "status": command.status,
                "deployment_id": (
                    str(command.deployment_id) if command.deployment_id else None
                ),
            }
        )


class AgentRuntimeDeploymentCheckpointView(APIView):
    authentication_classes = []
    permission_classes = []

    ALLOWED_PHASES = {
        "target_agent_bootstrap",
        "capi_pivot_preflight",
        "capi_pivot_verified",
        "worker_provisioning",
        "worker_pool_verified",
        "eph_reclaim_pending",
        "completed",
    }

    FORBIDDEN_CHECKPOINT_KEYS = FORBIDDEN_PAYLOAD_KEYS | {
        "authorization",
        "credential",
        "credentials",
        "access_token",
        "refresh_token",
        "client_secret",
    }

    def _deployment(self, request, deployment_id):
        node = _authenticate_node(request, request.data.get("node_id"))
        if (
            node is None
            or node.node_type != AgentNode.NodeType.TARGET_CLUSTER
            or node.status != AgentNode.Status.ONLINE
            or node.last_seen_at is None
            or node.last_seen_at < timezone.now() - datetime.timedelta(seconds=60)
        ):
            return None, None
        deployment = ClusterDeployment.objects.filter(
            pk=deployment_id,
            tenant=node.tenant,
            project=node.project,
            target_node=node,
        ).first()
        if deployment is None:
            return None, None
        if deployment.status not in {
            ClusterDeployment.Status.CONTROL_PLANE_READY,
            ClusterDeployment.Status.TARGET_AGENT_PENDING,
            ClusterDeployment.Status.POSTFLIGHT,
        }:
            return node, None
        return node, deployment

    def _validate_checkpoint(self, payload):
        if not isinstance(payload, dict):
            return "checkpoint data must be a JSON object."
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 32768:
            return "checkpoint data must not exceed 32 KiB."

        def visit(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).lower() in self.FORBIDDEN_CHECKPOINT_KEYS:
                        return False
                    if not visit(child):
                        return False
            if isinstance(value, list):
                return all(visit(item) for item in value)
            return True

        if not visit(payload):
            return "checkpoint contains a forbidden credential or executable field."
        return None

    def get(self, request, deployment_id):
        node_id = request.query_params.get("node_id")
        node = _authenticate_node(request, node_id)
        if (
            node is None
            or node.node_type != AgentNode.NodeType.TARGET_CLUSTER
            or node.status != AgentNode.Status.ONLINE
            or node.last_seen_at is None
            or node.last_seen_at < timezone.now() - datetime.timedelta(seconds=60)
        ):
            return Response({"detail": "Invalid or stale target agent credential."}, status=401)
        deployment = ClusterDeployment.objects.filter(
            pk=deployment_id,
            tenant=node.tenant,
            project=node.project,
            target_node=node,
        ).first()
        if deployment is None:
            return Response(
                {"detail": "Deployment is not assigned to this target agent."},
                status=404,
            )
        if deployment.status not in {
            ClusterDeployment.Status.CONTROL_PLANE_READY,
            ClusterDeployment.Status.TARGET_AGENT_PENDING,
            ClusterDeployment.Status.POSTFLIGHT,
        }:
            return Response({"detail": "Deployment is not in an agent handoff state."}, status=409)
        checkpoint = ClusterDeploymentCheckpoint.objects.filter(
            deployment=deployment,
            agent_node=node,
        ).first()
        if checkpoint is None:
            return Response(
                {
                    "deployment_id": str(deployment.id),
                    "generation": 1,
                    "sequence": 0,
                    "phase": "target_agent_bootstrap",
                    "data": {},
                    "resume_available": False,
                }
            )
        return Response(
            {
                "deployment_id": str(deployment.id),
                "generation": checkpoint.generation,
                "sequence": checkpoint.sequence,
                "phase": checkpoint.phase,
                "data": checkpoint.data,
                "resume_available": True,
                "updated_at": checkpoint.updated_at,
            }
        )

    def post(self, request, deployment_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node, deployment = self._deployment(request, deployment_id)
        if node is None:
            return Response({"detail": "Invalid target agent credential."}, status=401)
        if deployment is None:
            return Response(
                {
                    "detail": (
                        "Deployment is not assigned to this target agent or is not handoff-ready."
                    )
                },
                status=409,
            )
        phase = request.data.get("phase")
        generation = request.data.get("generation")
        sequence = request.data.get("sequence")
        checkpoint_data = request.data.get("data", {})
        if phase not in self.ALLOWED_PHASES:
            return Response({"detail": "Unsupported checkpoint phase."}, status=400)
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation != 1
        ):
            return Response(
                {"detail": "generation must match the active generation 1."},
                status=409,
            )
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            return Response({"detail": "sequence must be a positive integer."}, status=400)
        validation_error = self._validate_checkpoint(checkpoint_data)
        if validation_error:
            return Response({"detail": validation_error}, status=400)
        with transaction.atomic():
            deployment = ClusterDeployment.objects.select_for_update().get(pk=deployment.pk)
            if deployment.target_node_id != node.id or deployment.status not in {
                ClusterDeployment.Status.CONTROL_PLANE_READY,
                ClusterDeployment.Status.TARGET_AGENT_PENDING,
                ClusterDeployment.Status.POSTFLIGHT,
            }:
                return Response({"detail": "Deployment handoff ownership changed."}, status=409)
            checkpoint = ClusterDeploymentCheckpoint.objects.select_for_update().filter(
                deployment=deployment,
            ).first()
            if checkpoint is not None:
                if checkpoint.agent_node_id != node.id or checkpoint.generation != generation:
                    return Response(
                        {"detail": "Checkpoint generation or agent identity mismatch."},
                        status=409,
                    )
                if sequence < checkpoint.sequence:
                    return Response(
                        {"detail": "Checkpoint sequence must increase monotonically."},
                        status=409,
                    )
                if sequence == checkpoint.sequence:
                    if checkpoint.phase == phase and checkpoint.data == checkpoint_data:
                        return Response(
                            {
                                "deployment_id": str(deployment.id),
                                "generation": checkpoint.generation,
                                "sequence": checkpoint.sequence,
                                "phase": checkpoint.phase,
                                "idempotent": True,
                            }
                        )
                    return Response({"detail": "Checkpoint sequence was already used."}, status=409)
                checkpoint.sequence = sequence
                checkpoint.phase = phase
                checkpoint.data = checkpoint_data
                checkpoint.save(update_fields=["sequence", "phase", "data", "updated_at"])
            else:
                checkpoint = ClusterDeploymentCheckpoint.objects.create(
                    deployment=deployment,
                    agent_node=node,
                    generation=generation,
                    sequence=sequence,
                    phase=phase,
                    data=checkpoint_data,
                )
        return Response(
            {
                "deployment_id": str(deployment.id),
                "generation": checkpoint.generation,
                "sequence": checkpoint.sequence,
                "phase": checkpoint.phase,
                "idempotent": False,
            },
            status=201,
        )


class AgentProjectCommandListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(
            request,
            tenant_id,
            project_id,
        )
        try:
            limit = int(request.query_params.get("limit", "100"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return Response(
                {"detail": "limit and offset must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not 1 <= limit <= 200 or offset < 0:
            return Response(
                {"detail": "limit must be 1–200 and offset must be non-negative."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        commands = AgentCommand.objects.filter(
            node__tenant=tenant,
            node__project=project,
        ).select_related("node", "deployment")
        status_filter = request.query_params.get("status")
        if status_filter:
            if status_filter not in AgentCommand.Status.values:
                return Response(
                    {"detail": "Unknown command status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            commands = commands.filter(status=status_filter)
        node_id = request.query_params.get("node_id")
        if node_id:
            try:
                uuid.UUID(node_id)
            except (TypeError, ValueError, AttributeError):
                return Response(
                    {"detail": "node_id must be a UUID."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            commands = commands.filter(node_id=node_id)
        command_type = request.query_params.get("command_type")
        if command_type:
            commands = commands.filter(command_type=command_type)
        total = commands.count()
        page = commands.order_by("-created_at", "-id")[offset : offset + limit]
        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": [_command_data(command) for command in page],
            }
        )
