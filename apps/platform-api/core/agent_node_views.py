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

from core.cluster_deployment_services import handle_deployment_command_result
from core.models import (
    AgentCommand,
    AgentNode,
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
}
TARGET_COMMANDS = {
    "node.health",
    "cluster.health",
    "cluster.observe",
    "cluster.diagnose",
    "cluster.postflight",
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


def _validate_payload(payload):
    if not isinstance(payload, dict):
        return "payload must be a JSON object."
    if len(json.dumps(payload, ensure_ascii=False)) > 65536:
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
        return Response(
            {
                "node_id": str(node.id),
                "status": node.status,
                "server_time": node.last_seen_at,
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
        if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False)) > 65536:
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
