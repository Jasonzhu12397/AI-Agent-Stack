# ruff: noqa: I001
import datetime
import ipaddress
import hashlib
import hmac
import json
import re
import secrets
import ssl
import uuid
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.module_loading import import_string
import httpx
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from packages.agent_contracts.host_network_verify import verify_host_networks
from packages.agent_contracts.infra_networks import InfraNetworkError
from core.audit import record_audit_event
from core.models import (
    AgentCommand,
    AgentMachine,
    AgentNode,
    ClusterDeployment,
    FirmwareUpdateJob,
    InfrastructureNetworkProfile,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)


def _network_observation(inventory: Any, result: Any) -> dict[str, Any] | None:
    """Return the OS network fields from a succeeded observation command."""

    if not isinstance(inventory, dict) or not isinstance(result, dict):
        return None
    output = result.get("output")
    if not isinstance(output, dict):
        return None
    nics = output.get("nics")
    bonds = output.get("bonds")
    if not isinstance(nics, list) or not isinstance(bonds, list):
        return None
    if not all(isinstance(item, dict) for item in nics + bonds):
        return None
    return {
        **inventory,
        "nics": nics,
        "bonds": bonds,
        "network_observation": {
            "source": "host.network.observe",
            "nics": len(nics),
            "bonds": len(bonds),
        },
    }


EPH_COMMANDS = {
    "host.network.observe",
    "node.health",
    "host.preflight",
    "cluster.plan",
    "cluster.health",
    "cluster.network.inspect",
    "cluster.observe",
    "cluster.diagnose",
    "cluster.postflight",
    "k8s.resource.read",
    "capi.cluster.observe",
    "metal3.host.inspect",
    "cluster.provider.discover",
    "cluster.provider.compatibility",
    "cluster.manifest.validate",
    "cluster.capi.pivot.source.preflight",
    "cluster.capi.pivot.dry-run",
    "cluster.capi.pivot.execute",
}
TARGET_COMMANDS = {
    "host.network.observe",
    "node.health",
    "cluster.provider.compatibility",
    "cluster.health",
    "cluster.network.inspect",
    "cluster.observe",
    "cluster.diagnose",
    "cluster.postflight",
    "cluster.control-plane.verify",
    "cluster.capi.pivot.target.preflight",
    "cluster.capi.pivot.target.verify",
    "cluster.capi.pivot.postflight",
    "cluster.capi.worker.preflight",
    "cluster.capi.worker.provision",
    "cluster.delivery.lifecycle.evaluate",
    "cluster.kubernetes.version.upgrade",
    "cluster.metal3.observe",
    "k8s.resource.read",
}
MUTATING_COMMANDS = {
    "cluster.capi.pivot.execute",
    "cluster.capi.worker.provision",
    "cluster.kubernetes.version.upgrade",
}
APPROVAL_REQUIRED_COMMANDS = {
    "cluster.kubernetes.version.upgrade",
}
SUPPORTED_KUBERNETES_MINOR_VERSIONS = frozenset({"v1.33", "v1.34", "v1.35", "v1.36"})

def _cancel_agent_commands(queryset, now):
    unknown_outcomes = []
    commands = queryset.select_for_update().order_by("id")
    for command in commands:
        if (
            command.status == AgentCommand.Status.RUNNING
            and command.command_type in MUTATING_COMMANDS
        ):
            command.status = AgentCommand.Status.FAILED
            command.error = {
                "code": "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED",
                "retryable": False,
            }
            unknown_outcomes.append(str(command.id))
        else:
            command.status = AgentCommand.Status.CANCELLED
            command.error = None
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
            ]
        )
    return unknown_outcomes

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


def _command_is_fenced_by_cluster_ownership(command, node):
    if node.node_type != AgentNode.NodeType.EPH_NODE or command.deployment_id is None:
        return False
    deployment = ClusterDeployment.objects.select_for_update().filter(
        pk=command.deployment_id,
    ).first()
    if deployment is None:
        return True
    plan_result = deployment.plan_result
    if plan_result is None:
        return False
    if not isinstance(plan_result, dict):
        return False
    dual_agent = plan_result.get("dual_agent", {})
    return isinstance(dual_agent, dict) and (
        dual_agent.get("target_agent_owns_cluster") is True
        or dual_agent.get("pivot_executed") is True
    )


def _fail_fenced_command(command, now):
    command.status = AgentCommand.Status.FAILED
    command.error = {
        "code": "AGENT_CONTROL_OWNERSHIP_FENCED",
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
        "client_certificate": {
            "fingerprint_sha256": node.client_certificate_fingerprint or None,
            "bound_at": node.client_certificate_bound_at,
            "revoked_at": node.client_certificate_revoked_at,
            "status": (
                "active"
                if node.client_certificate_fingerprint
                and node.client_certificate_revoked_at is None
                else "revoked"
                if node.client_certificate_revoked_at is not None
                else "unbound"
            ),
        },
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


def _validate_kubernetes_upgrade_result(command, result):
    if command.command_type != "cluster.kubernetes.version.upgrade":
        return None
    if not isinstance(result, dict):
        return "Kubernetes upgrade result must be an object."
    expected_identity = {
        "cluster_name": command.payload.get("cluster_name"),
        "current_version": command.payload.get("current_version"),
        "target_version": command.payload.get("target_version"),
    }
    if any(result.get(key) != value for key, value in expected_identity.items()):
        return "Kubernetes upgrade result does not match the approved command."
    if (
        result.get("approval_required") is not True
        or result.get("upgrade_simulated") is not True
        or result.get("upgrade_performed") is not False
        or result.get("side_effects") is not False
        or result.get("requires_manual_execution") is not True
        or result.get("resume_supported") is not False
    ):
        return "Kubernetes upgrade result violates the simulation-only safety contract."
    expected_steps = [
        ("preflight", "not_run"),
        ("control_plane", "not_started"),
        ("workers", "not_started"),
        ("postflight", "not_started"),
    ]
    steps = result.get("upgrade_steps")
    if not isinstance(steps, list) or len(steps) != len(expected_steps):
        return "Kubernetes upgrade plan must contain the four ordered, unexecuted stages."
    for index, (name, status_value) in enumerate(expected_steps):
        step = steps[index]
        if (
            not isinstance(step, dict)
            or step.get("sequence") != index + 1
            or step.get("name") != name
            or step.get("status") != status_value
        ):
            return "Kubernetes upgrade plan stages must remain ordered and unexecuted."
    return None


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
    if (
        node is None
        or node.status == AgentNode.Status.REVOKED
        or node.client_certificate_revoked_at is not None
    ):
        return None
    expected_fingerprint = node.client_certificate_fingerprint
    if getattr(settings, "AGENT_MTLS_REQUIRED", False) and not expected_fingerprint:
        return None
    if expected_fingerprint:
        remote_addr = request.META.get("REMOTE_ADDR", "")
        trusted_proxy_ip = getattr(settings, "AGENT_MTLS_TRUSTED_PROXY_IP", "")
        try:
            request_peer = ipaddress.ip_address(remote_addr)
            if trusted_proxy_ip:
                trusted_proxy = request_peer == ipaddress.ip_address(trusted_proxy_ip)
            else:
                trusted_proxy_values = getattr(
                    settings,
                    "AGENT_MTLS_TRUSTED_PROXY_CIDRS",
                    [],
                )
                trusted_proxy = any(
                    request_peer in ipaddress.ip_network(value, strict=False)
                    for value in trusted_proxy_values
                )
        except ValueError:
            return None
        if not trusted_proxy:
            return None
        verified_fingerprint = request.headers.get(
            "X-Verified-Client-Cert-SHA256",
            "",
        ).strip().lower()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", verified_fingerprint)
            or not hmac.compare_digest(verified_fingerprint, expected_fingerprint)
        ):
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


class FleetMachineInventoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except (TypeError, ValueError):
            return Response({"detail": "limit and offset must be integers."}, status=400)
        if not 1 <= limit <= 200 or offset < 0:
            return Response(
                {"detail": "limit must be 1–200 and offset must be non-negative."},
                status=400,
            )

        nodes = AgentNode.objects.filter(tenant=tenant, project=project)
        node_type = request.query_params.get("node_type")
        if node_type:
            if node_type not in AgentNode.NodeType.values:
                return Response({"detail": "Unknown node_type."}, status=400)
            nodes = nodes.filter(node_type=node_type)

        requested_status = request.query_params.get("status")
        if requested_status:
            if requested_status not in {
                AgentNode.Status.ONLINE,
                AgentNode.Status.OFFLINE,
                AgentNode.Status.REVOKED,
                "stale",
            }:
                return Response({"detail": "Unknown machine status."}, status=400)
            if requested_status == "stale":
                stale_before = timezone.now() - datetime.timedelta(seconds=60)
                nodes = nodes.filter(status=AgentNode.Status.ONLINE).filter(
                    Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=stale_before)
                )
            else:
                nodes = nodes.filter(status=requested_status)

        search = request.query_params.get("search", "").strip()
        if search:
            nodes = nodes.filter(
                Q(name__icontains=search)
                | Q(cluster_name__icontains=search)
                | Q(agent_version__icontains=search)
            )

        total = nodes.count()
        page = nodes.order_by("name", "id")[offset : offset + limit]
        stale_before = timezone.now() - datetime.timedelta(seconds=60)
        results = []
        for node in page:
            data = _node_data(node)
            data["observed_status"] = (
                "stale"
                if node.status == AgentNode.Status.ONLINE
                and (node.last_seen_at is None or node.last_seen_at < stale_before)
                else node.status
            )
            machine = AgentMachine.objects.filter(agent_node=node).first()
            data["identity"] = {
                "agent_node_id": str(node.id),
                "machine_id": str(machine.id) if machine else None,
                "name": node.name,
                "cluster_name": node.cluster_name,
            }
            data["hardware_inventory"] = (
                machine.hardware_inventory if machine else node.labels.get("hardware_inventory", {})
            )
            data["inventory_source"] = (
                machine.inventory_source if machine else "legacy_agent_label"
            )
            data["identity"]["system_uuid"] = machine.system_uuid if machine else ""
            data["identity"]["identity_status"] = (
                machine.identity_status if machine else "unverified"
            )
            data["identity"]["identity_fingerprint"] = (
                machine.identity_fingerprint or None if machine else None
            )
            data["identity"]["identity_conflicts"] = (
                machine.identity_conflicts if machine else []
            )
            data["observed_at"] = machine.observed_at if machine else node.last_seen_at
            results.append(data)

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": results,
                "capabilities": {
                    "hardware_inventory": "agent_reported_when_available",
                    "stable_hardware_identity": "not_yet_attested",
                },
            }
        )


def _identity_value(value):
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    if normalized in {
        "", "none", "unknown", "default string", "to be filled by o.e.m.",
        "not specified", "not applicable", "00000000-0000-0000-0000-000000000000",
    }:
        return ""
    return normalized[:256]



def _validate_bmc_endpoint(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (port is not None and not 1 <= port <= 65535)
    ):
        return None
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host in {"localhost"} or normalized_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        return None
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            normalized_host,
        ):
            return None
    else:
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return None
    return parsed, normalized_host


def _machine_identity_material(system_identity):
    fields = {
        "product_uuid": 128,
        "product_serial": 128,
        "board_serial": 128,
        "chassis_serial": 128,
    }
    material = {}
    for key, max_length in fields.items():
        value = _identity_value(system_identity.get(key))
        if value:
            material[key] = value[:max_length]
    return material


def _machine_conflicts(node, material, machine):
    if not material:
        return []
    conflicts = []
    candidates = AgentMachine.objects.filter(
        tenant=node.tenant,
        project=node.project,
    ).exclude(agent_node=node).only("id", "agent_node_id", "identity_material")
    for candidate in candidates.iterator():
        other = candidate.identity_material or {}
        shared = sorted(
            key for key, value in material.items()
            if value and other.get(key) == value
        )
        if shared:
            conflicts.append({
                "machine_id": str(candidate.id),
                "agent_node_id": str(candidate.agent_node_id),
                "matching_fields": shared,
            })
    return conflicts



def _record_bmc_verification_failure(request, tenant, project, machine_id, reason):
    observed = timezone.now().isoformat()
    with transaction.atomic():
        machine = AgentMachine.objects.select_for_update().get(
            pk=machine_id,
            tenant=tenant,
            project=project,
        )
        machine.bmc_identity_evidence = {
            "status": "error",
            "source": "redfish",
            "observed_at": observed,
            "reason": reason,
        }
        machine.save(
            update_fields=["bmc_identity_evidence", "updated_at"]
        )
        record_audit_event(
            request,
            tenant,
            "fleet.machine.bmc_identity_check_failed",
            "agent_machine",
            machine.id,
            project=project,
            metadata={
                "verification_status": "error",
                "reason": reason,
            },
        )


class FleetMachineBMCConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, tenant_id, project_id, machine_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        allowed = {"endpoint", "credential_ref", "trust_bundle_ref"}
        if set(request.data) - allowed:
            return Response(
                {"detail": "Only endpoint and credential references are accepted."},
                status=400,
            )
        tenant, project, is_tenant_admin = _tenant_and_project(
            request,
            tenant_id,
            project_id,
        )
        project_editor = ProjectMembership.objects.filter(
            project=project,
            user=request.user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
        if not is_tenant_admin and not project_editor:
            return Response(
                {"detail": "Project editor or administrator role is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        endpoint = _validate_bmc_endpoint(request.data.get("endpoint"))
        credential_ref = request.data.get("credential_ref")
        trust_bundle_ref = request.data.get("trust_bundle_ref", "")
        reference_pattern = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}"
        if endpoint is None:
            return Response(
                {"detail": "endpoint must be an HTTPS origin with a valid host."},
                status=400,
            )
        if (
            not isinstance(credential_ref, str)
            or not re.fullmatch(reference_pattern, credential_ref)
        ):
            return Response(
                {"detail": "credential_ref must be a vault reference, not a secret."},
                status=400,
            )
        if trust_bundle_ref and (
            not isinstance(trust_bundle_ref, str)
            or not re.fullmatch(reference_pattern, trust_bundle_ref)
        ):
            return Response({"detail": "Invalid trust_bundle_ref."}, status=400)
        parsed, hostname = endpoint
        with transaction.atomic():
            machine = AgentMachine.objects.select_for_update().filter(
                pk=machine_id,
                tenant=tenant,
                project=project,
            ).first()
            if machine is None:
                return Response({"detail": "Machine not found."}, status=404)
            machine.bmc_endpoint = f"https://{parsed.netloc}"
            machine.bmc_credential_ref = credential_ref
            machine.bmc_trust_bundle_ref = trust_bundle_ref
            machine.bmc_tls_verify = True
            machine.bmc_configured_at = timezone.now()
            machine.bmc_identity_evidence = {
                "status": "not_checked",
                "source": "redfish",
                "configured_host": hostname,
            }
            machine.save(
                update_fields=[
                    "bmc_endpoint",
                    "bmc_credential_ref",
                    "bmc_trust_bundle_ref",
                    "bmc_tls_verify",
                    "bmc_configured_at",
                    "bmc_identity_evidence",
                    "updated_at",
                ]
            )
            record_audit_event(
                request,
                tenant,
                "fleet.machine.bmc_configured",
                "agent_machine",
                machine.id,
                project=project,
                metadata={
                    "configured_host": hostname,
                    "tls_verify": True,
                    "credential_reference_configured": True,
                    "trust_bundle_reference_configured": bool(trust_bundle_ref),
                },
            )
        return Response(
            {
                "machine_id": str(machine.id),
                "bmc_configured": True,
                "endpoint": machine.bmc_endpoint,
                "credential_configured": bool(machine.bmc_credential_ref),
                "trust_bundle_configured": bool(machine.bmc_trust_bundle_ref),
                "tls_verify": machine.bmc_tls_verify,
                "verification_status": machine.bmc_identity_evidence["status"],
                "network_check_performed": False,
            },
            status=200,
        )


    def post(self, request, tenant_id, project_id, machine_id):
        if request.data not in ({}, None):
            return Response(
                {"detail": "This endpoint does not accept a request body."},
                status=400,
            )
        tenant, project, is_tenant_admin = _tenant_and_project(
            request,
            tenant_id,
            project_id,
        )
        project_editor = ProjectMembership.objects.filter(
            project=project,
            user=request.user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
        if not is_tenant_admin and not project_editor:
            return Response(
                {"detail": "Project editor or administrator role is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        machine = AgentMachine.objects.filter(
            pk=machine_id,
            tenant=tenant,
            project=project,
        ).first()
        if machine is None:
            return Response({"detail": "Machine not found."}, status=404)
        if not machine.bmc_endpoint or not machine.bmc_credential_ref:
            return Response(
                {"detail": "BMC endpoint and credential reference must be configured."},
                status=409,
            )
        resolver_path = getattr(settings, "BMC_CREDENTIAL_RESOLVER", "")
        if (
            not resolver_path
            and getattr(settings, "BMC_VAULT_ADDR", "")
            and (
                getattr(settings, "BMC_VAULT_TOKEN", "")
                or getattr(settings, "BMC_VAULT_TOKEN_RESOLVER", "")
                or getattr(settings, "BMC_VAULT_JWT_PATH", "")
            )
        ):
            resolver_path = "core.bmc_credentials.resolve_bmc_credentials"
        if not resolver_path:
            observed = timezone.now().isoformat()
            with transaction.atomic():
                machine = AgentMachine.objects.select_for_update().get(
                    pk=machine_id,
                    tenant=tenant,
                    project=project,
                )
                machine.bmc_identity_evidence = {
                    "status": "not_checked",
                    "source": "redfish",
                    "observed_at": observed,
                    "reason": "credential_resolver_not_configured",
                }
                machine.save(
                    update_fields=["bmc_identity_evidence", "updated_at"]
                )
                record_audit_event(
                    request,
                    tenant,
                    "fleet.machine.bmc_identity_check_skipped",
                    "agent_machine",
                    machine.id,
                    project=project,
                    metadata={
                        "verification_status": "not_checked",
                        "reason": "credential_resolver_not_configured",
                    },
                )
            return Response(
                {
                    "detail": "BMC credential resolver is not configured.",
                    "verification_status": "not_checked",
                    "reason": "credential_resolver_not_configured",
                },
                status=503,
            )
        try:
            resolver = import_string(resolver_path)
            resolved = resolver(
                credential_ref=machine.bmc_credential_ref,
                trust_bundle_ref=machine.bmc_trust_bundle_ref,
                tenant_id=str(tenant.id),
                project_id=str(project.id),
            )
            if not isinstance(resolved, dict):
                raise ValueError("BMC credential resolver returned an invalid value.")
            credential = {
                "username": resolved.get("username"),
                "password": resolved.get("password"),
            }
            trust_bundle_pem = resolved.get("trust_bundle_pem", "")
            if trust_bundle_pem and not isinstance(trust_bundle_pem, str):
                raise ValueError("BMC trust bundle resolver returned an invalid value.")
            systems = _redfish_systems(machine.bmc_endpoint, credential, trust_bundle_pem)
        except PermissionError:
            _record_bmc_verification_failure(
                request, tenant, project, machine_id, "authentication_failed"
            )
            return Response({"detail": "BMC authentication failed."}, status=502)
        except (ValueError, ImportError, AttributeError):
            _record_bmc_verification_failure(
                request, tenant, project, machine_id, "invalid_response_or_configuration"
            )
            return Response({"detail": "BMC verification configuration is invalid."}, status=400)
        except httpx.TimeoutException:
            _record_bmc_verification_failure(
                request, tenant, project, machine_id, "request_timeout"
            )
            return Response({"detail": "BMC verification timed out."}, status=504)
        except httpx.HTTPError:
            _record_bmc_verification_failure(
                request, tenant, project, machine_id, "connection_or_tls_failure"
            )
            return Response({"detail": "BMC connection or TLS verification failed."}, status=502)
        except RuntimeError:
            _record_bmc_verification_failure(
                request, tenant, project, machine_id, "redfish_request_failed"
            )
            return Response({"detail": "Redfish request failed."}, status=502)
        verification_status, conflicts, reason = _redfish_identity_review(machine, systems)
        observed = timezone.now().isoformat()
        system = systems[0] if len(systems) == 1 else {}
        with transaction.atomic():
            machine = AgentMachine.objects.select_for_update().get(
                pk=machine_id,
                tenant=tenant,
                project=project,
            )
            machine.bmc_identity_evidence = {
                "status": verification_status,
                "source": "redfish_tls_verified",
                "observed_at": observed,
                "reason": reason,
                "system": system,
                "conflicts": conflicts,
            }
            if verification_status == "verified":
                machine.identity_status = AgentMachine.IdentityStatus.VERIFIED
            elif verification_status == "conflict":
                machine.identity_status = AgentMachine.IdentityStatus.CONFLICT
                machine.identity_conflicts = list(machine.identity_conflicts or []) + [
                    {
                        "type": "bmc_identity_conflict",
                        "source": "redfish",
                        "fields": conflicts,
                        "observed_at": observed,
                    }
                ]
            machine.save(
                update_fields=[
                    "bmc_identity_evidence",
                    "identity_status",
                    "identity_conflicts",
                    "updated_at",
                ]
            )
            record_audit_event(
                request,
                tenant,
                "fleet.machine.bmc_identity_checked",
                "agent_machine",
                machine.id,
                project=project,
                metadata={
                    "verification_status": verification_status,
                    "reason": reason,
                    "conflict_fields": conflicts,
                },
            )
        return Response(
            {
                "machine_id": str(machine.id),
                "verification_status": verification_status,
                "identity_status": machine.identity_status,
                "reason": reason,
                "conflicts": conflicts,
                "system": system,
            },
            status=200,
        )


def _redfish_read_json(client, url, auth):
    with client.stream("GET", url, auth=auth) as response:
        if 300 <= response.status_code < 400:
            raise ValueError("Redfish redirects are not allowed.")
        if response.status_code == 401 or response.status_code == 403:
            raise PermissionError("BMC authentication failed.")
        if response.status_code != 200:
            raise RuntimeError("Redfish endpoint returned an unsuccessful status.")
        content_type = (
            response.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        if content_type != "application/json":
            raise ValueError("Redfish endpoint did not return JSON.")
        chunks = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 1048576:
                raise ValueError("Redfish response exceeded the size limit.")
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Redfish endpoint returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Redfish response must be a JSON object.")
        return payload


def _redfish_systems(endpoint, credential, trust_bundle_pem):
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError(
            "Redfish verification currently requires a literal BMC IP address."
        ) from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("Redfish verification only permits private BMC IP addresses.")
    if not isinstance(credential, dict):
        raise ValueError("BMC credential resolver returned an invalid value.")
    username = credential.get("username")
    password = credential.get("password")
    if (
        not isinstance(username, str)
        or not username
        or not isinstance(password, str)
        or not password
    ):
        raise ValueError("BMC credential resolver did not return username and password.")
    context = ssl.create_default_context(cadata=trust_bundle_pem or None)
    auth = httpx.BasicAuth(username, password)
    base_url = f"https://{parsed.netloc}"
    systems_url = f"{base_url}/redfish/v1/Systems"
    results = []
    with httpx.Client(
        verify=context,
        timeout=httpx.Timeout(5.0, connect=3.0),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        collection = _redfish_read_json(client, systems_url, auth)
        members = collection.get("Members")
        if not isinstance(members, list) or not members:
            raise ValueError("Redfish Systems collection contains no members.")
        if len(members) > 8:
            raise ValueError("Redfish Systems collection exceeds the supported member limit.")
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("Redfish Systems collection contains an invalid member.")
            member_path = member.get("@odata.id")
            if not isinstance(member_path, str) or not re.fullmatch(
                r"/redfish/v1/Systems/[A-Za-z0-9_.-]{1,64}", member_path
            ):
                raise ValueError("Redfish Systems member path is invalid.")
            system = _redfish_read_json(client, f"{base_url}{member_path}", auth)
            results.append(
                {
                    "uuid": _identity_value(system.get("UUID")),
                    "serial": _identity_value(system.get("SerialNumber")),
                    "manufacturer": _identity_value(system.get("Manufacturer")),
                    "model": _identity_value(system.get("Model")),
                }
            )
    return results


def _redfish_identity_review(machine, systems):
    if machine.identity_status == AgentMachine.IdentityStatus.CONFLICT:
        return "conflict", [], "existing_identity_conflict_requires_review"
    material = machine.identity_material or {}
    dmi_uuid = _identity_value(machine.system_uuid or material.get("product_uuid"))
    dmi_serial = _identity_value(material.get("product_serial"))
    if len(systems) != 1:
        return "conflict", [], "expected_exactly_one_system"
    system = systems[0]
    conflicts = []
    if dmi_uuid and system["uuid"] and dmi_uuid.lower() != system["uuid"].lower():
        conflicts.append("system_uuid")
    if dmi_serial and system["serial"] and dmi_serial.lower() != system["serial"].lower():
        conflicts.append("product_serial")
    if conflicts:
        return "conflict", conflicts, "dmi_redfish_mismatch"
    if not dmi_uuid or not dmi_serial or not system["uuid"] or not system["serial"]:
        return "candidate", [], "insufficient_identity_material"
    if dmi_uuid.lower() != system["uuid"].lower() or dmi_serial.lower() != system["serial"].lower():
        return "conflict", ["system_uuid", "product_serial"], "dmi_redfish_mismatch"
    duplicates = AgentMachine.objects.filter(
        tenant=machine.tenant,
        project=machine.project,
    ).exclude(pk=machine.pk).filter(
        Q(system_uuid__iexact=system["uuid"])
        | Q(identity_material__product_serial__iexact=system["serial"])
        | Q(identity_material__product_uuid__iexact=system["uuid"])
    )
    if duplicates.exists():
        return "conflict", ["system_uuid", "product_serial"], "duplicate_machine_identity"
    return "verified", [], "dmi_redfish_match"



def _bmc_credential_for(machine, tenant, project):
    resolver_path = getattr(settings, "BMC_CREDENTIAL_RESOLVER", "")
    if (
        not resolver_path
        and getattr(settings, "BMC_VAULT_ADDR", "")
        and (
            getattr(settings, "BMC_VAULT_TOKEN", "")
            or getattr(settings, "BMC_VAULT_TOKEN_RESOLVER", "")
            or getattr(settings, "BMC_VAULT_JWT_PATH", "")
        )
    ):
        resolver_path = "core.bmc_credentials.resolve_bmc_credentials"
    if not resolver_path:
        raise RuntimeError("BMC credential resolver is not configured.")
    resolver = import_string(resolver_path)
    resolved = resolver(
        credential_ref=machine.bmc_credential_ref,
        trust_bundle_ref=machine.bmc_trust_bundle_ref,
        tenant_id=str(tenant.id),
        project_id=str(project.id),
    )
    if not isinstance(resolved, dict):
        raise ValueError("BMC credential resolver returned an invalid value.")
    username = resolved.get("username")
    password = resolved.get("password")
    if not isinstance(username, str) or not username:
        raise ValueError("BMC credential resolver did not return a username.")
    if not isinstance(password, str) or not password:
        raise ValueError("BMC credential resolver did not return a password.")
    trust_bundle_pem = resolved.get("trust_bundle_pem", "")
    if trust_bundle_pem and not isinstance(trust_bundle_pem, str):
        raise ValueError("BMC trust bundle resolver returned an invalid value.")
    return {"username": username, "password": password}, trust_bundle_pem


FIRMWARE_SUPPORTED_VENDORS = (
    "HPE",
    "Dell",
    "Inspur",
    "H3C",
    "Lenovo",
)
FIRMWARE_VENDOR_UNSUPPORTED_DETAIL = (
    "Firmware management supports HPE, Dell, Inspur, H3C and Lenovo BMCs only."
)


def _firmware_vendor_scope(machine):
    """Return the canonical firmware vendor for a supported machine."""

    values = " ".join(
        str(value or "") for value in (machine.vendor, machine.product_name)
    ).casefold()
    if not values.strip():
        return None
    tokens = set(re.split(r"[^a-z0-9]+", values))
    if "hewlett" in values or "hpe" in values or "hp" in tokens:
        return "HPE"
    if "dell" in values:
        return "Dell"
    if "inspur" in values or "浪潮" in values:
        return "Inspur"
    if "h3c" in values or "新华三" in values:
        return "H3C"
    if "lenovo" in values or "联想" in values:
        return "Lenovo"
    return None


def _firmware_vendor_gate(machine):
    if _firmware_vendor_scope(machine) is None:
        return Response(
            {
                "detail": FIRMWARE_VENDOR_UNSUPPORTED_DETAIL,
                "supported_vendors": list(FIRMWARE_SUPPORTED_VENDORS),
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return None


def _firmware_client(machine, credential, trust_bundle_pem):
    parsed = urlsplit(machine.bmc_endpoint)
    hostname = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError(
            "Firmware management requires a literal BMC IP address."
        ) from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("Firmware management only permits private BMC IP addresses.")
    context = ssl.create_default_context(cadata=trust_bundle_pem or None)
    auth = httpx.BasicAuth(credential["username"], credential["password"])
    client = httpx.Client(
        verify=context,
        timeout=httpx.Timeout(10.0, connect=3.0),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    )
    return client, f"https://{parsed.netloc}", auth


def _redfish_get_document(client, base_url, path, auth):
    if not isinstance(path, str) or not re.fullmatch(
        r"/redfish/v1/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,240}", path
    ):
        raise ValueError("Redfish document path is invalid.")
    return _redfish_read_json(client, f"{base_url}{path}", auth)


def _redfish_member_limit(paths):
    if not isinstance(paths, list):
        return []
    if len(paths) > 32:
        raise ValueError("Redfish firmware inventory exceeds the supported member limit.")
    result = []
    for item in paths:
        if not isinstance(item, dict):
            raise ValueError("Redfish firmware inventory contains an invalid member.")
        path = item.get("@odata.id")
        if not isinstance(path, str):
            raise ValueError("Redfish firmware inventory member path is invalid.")
        result.append(path)
    return result


def _firmware_inventory(machine, credential, trust_bundle_pem):
    client, base_url, auth = _firmware_client(machine, credential, trust_bundle_pem)
    with client:
        service = _redfish_get_document(
            client, base_url, "/redfish/v1/UpdateService", auth
        )
        inventory_link = service.get("FirmwareInventory")
        firmware = []
        if isinstance(inventory_link, dict):
            collection_path = inventory_link.get("@odata.id")
            collection = _redfish_get_document(
                client, base_url, collection_path, auth
            )
            for member_path in _redfish_member_limit(collection.get("Members")):
                member = _redfish_get_document(client, base_url, member_path, auth)
                status = member.get("Status") if isinstance(member.get("Status"), dict) else {}
                firmware.append(
                    {
                        "id": member.get("@odata.id", member_path),
                        "name": member.get("Name", ""),
                        "version": member.get("Version", ""),
                        "updateable": bool(member.get("Updateable", False)),
                        "status": status.get("Health", ""),
                    }
                )
        service_status = service.get("Status") if isinstance(service.get("Status"), dict) else {}
        return {
            "service_enabled": bool(service.get("ServiceEnabled", False)),
            "service_state": service_status.get("State", ""),
            "firmware": firmware,
        }


def _firmware_action_target(service, base_url):
    actions = service.get("Actions")
    if not isinstance(actions, dict):
        raise ValueError("Redfish UpdateService does not advertise SimpleUpdate.")
    simple_update = actions.get("#UpdateService.SimpleUpdate")
    if not isinstance(simple_update, dict):
        raise ValueError("Redfish UpdateService does not advertise SimpleUpdate.")
    target = simple_update.get("target")
    if not isinstance(target, str) or not target.startswith(f"{base_url}/redfish/v1/"):
        raise ValueError("Redfish SimpleUpdate target is invalid.")
    return target


def _validate_firmware_image_url(value):
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("image_url must be a non-empty HTTPS URL.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("image_url must be an HTTPS URL without embedded credentials.")
    if parsed.fragment:
        raise ValueError("image_url must not contain a fragment.")
    return value


def _sanitize_image_url(value):
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _firmware_simple_update(
    machine, credential, trust_bundle_pem, image_url, targets, apply_time
):
    client, base_url, auth = _firmware_client(machine, credential, trust_bundle_pem)
    with client:
        service = _redfish_get_document(
            client, base_url, "/redfish/v1/UpdateService", auth
        )
        if not bool(service.get("ServiceEnabled", False)):
            raise RuntimeError("Redfish UpdateService is disabled.")
        target = _firmware_action_target(service, base_url)
        payload = {"ImageURI": image_url, "TransferProtocol": "HTTPS"}
        if targets:
            payload["Targets"] = targets
        payload["@Redfish.OperationApplyTime"] = apply_time
        response = client.post(target, json=payload, auth=auth)
        if 300 <= response.status_code < 400:
            raise ValueError("Redfish SimpleUpdate redirects are not allowed.")
        if response.status_code in (401, 403):
            raise PermissionError("BMC authentication failed.")
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"Redfish SimpleUpdate returned HTTP {response.status_code}."
            )
        monitor = response.headers.get("location", "")
        if not monitor:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if isinstance(body, dict):
                monitor = body.get("@odata.id", "")
        if not monitor:
            raise RuntimeError("Redfish SimpleUpdate did not return a task monitor.")
        if not isinstance(monitor, str) or not monitor.startswith(f"{base_url}/redfish/v1/"):
            raise ValueError("Redfish SimpleUpdate task monitor is invalid.")
        return {
            "task_monitor": monitor,
            "task_state": "",
            "message": "Firmware update submitted to the BMC.",
        }


def _refresh_firmware_job(job):
    if job.status in (FirmwareUpdateJob.Status.SUCCEEDED, FirmwareUpdateJob.Status.FAILED):
        return job
    if not job.task_monitor:
        return job
    credential, trust_bundle_pem = _bmc_credential_for(
        job.machine, job.tenant, job.project
    )
    client, base_url, auth = _firmware_client(job.machine, credential, trust_bundle_pem)
    if not job.task_monitor.startswith(f"{base_url}/redfish/v1/"):
        raise ValueError("Firmware task monitor does not match the machine BMC.")
    monitor_path = job.task_monitor[len(base_url) :]
    with client:
        task = _redfish_get_document(client, base_url, monitor_path, auth)
    task_state = task.get("TaskState", "")
    task_status = task.get("TaskStatus", "")
    messages = task.get("Messages")
    message = ""
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            message = str(first.get("Message", ""))[:500]
    if task_state == "Completed":
        job.status = FirmwareUpdateJob.Status.SUCCEEDED
        job.finished_at = timezone.now()
    elif task_state in ("Exception", "Cancelled", "Interrupted", "Killed"):
        job.status = FirmwareUpdateJob.Status.FAILED
        job.finished_at = timezone.now()
    elif task_state:
        job.status = FirmwareUpdateJob.Status.RUNNING
    job.task_state = str(task_state)[:64]
    if task_status:
        job.message = f"{task_status}: {message}".strip()
    elif message:
        job.message = message
    job.save(
        update_fields=["status", "task_state", "message", "finished_at", "updated_at"]
    )
    return job


def _firmware_job_data(job):
    return {
        "id": str(job.id),
        "machine_id": str(job.machine_id),
        "status": job.status,
        "image_url": job.image_url,
        "component": job.component,
        "apply_time": job.apply_time,
        "targets": job.targets,
        "task_state": job.task_state,
        "message": job.message,
        "task_monitor": job.task_monitor,
        "requested_by": job.requested_by.get_username(),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _firmware_error_response(exc):
    if isinstance(exc, PermissionError):
        return Response({"detail": "BMC authentication failed."}, status=502)
    if isinstance(exc, httpx.TimeoutException):
        return Response({"detail": "BMC firmware request timed out."}, status=504)
    if isinstance(exc, httpx.HTTPError):
        return Response(
            {"detail": "BMC connection or TLS verification failed."}, status=502
        )
    if isinstance(exc, (ValueError, ImportError, AttributeError)):
        return Response({"detail": str(exc)}, status=400)
    if isinstance(exc, RuntimeError):
        return Response({"detail": str(exc)}, status=502)
    return Response({"detail": "Firmware management request failed."}, status=500)


class FleetMachineNetworkVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id, machine_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        allowed = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists() or ProjectMembership.objects.filter(
            project=project,
            user=request.user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
        if not allowed:
            return Response({"detail": "Project editor or admin role required."}, status=403)
        machine = AgentMachine.objects.filter(
            pk=machine_id, tenant=tenant, project=project
        ).first()
        if machine is None:
            return Response({"detail": "Machine not found."}, status=404)
        profile_record = InfrastructureNetworkProfile.objects.filter(
            tenant=tenant, project=project
        ).first()
        if profile_record is None:
            return Response(
                {"detail": "Infrastructure network profile is not configured."},
                status=409,
            )
        host_role = request.query_params.get("host_role", "worker")
        if host_role not in {"control_plane", "worker", "storage"}:
            return Response({"detail": "Unknown host_role."}, status=400)
        try:
            report = verify_host_networks(
                profile_record.profile,
                host_role,
                machine.hardware_inventory or {},
            )
        except InfraNetworkError as exc:
            return Response(
                {"detail": f"Infrastructure network profile is invalid: {exc.code}"},
                status=409,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(
            {
                "machine_id": str(machine.id),
                "machine_name": machine.agent_node.name,
                "inventory_source": machine.inventory_source,
                "inventory_revision": machine.inventory_revision,
                "observed_at": machine.observed_at.isoformat()
                if machine.observed_at
                else None,
                **report,
            },
            status=200,
        )


class FleetMachineFirmwareView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id, machine_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        machine = AgentMachine.objects.filter(
            pk=machine_id, tenant=tenant, project=project
        ).first()
        if machine is None:
            return Response({"detail": "Machine not found."}, status=404)
        vendor_gate = _firmware_vendor_gate(machine)
        if vendor_gate is not None:
            return vendor_gate
        jobs = machine.firmware_update_jobs.all()[:20]
        base = {
            "machine_id": str(machine.id),
            "bmc_configured": bool(machine.bmc_endpoint and machine.bmc_credential_ref),
            "jobs": [_firmware_job_data(job) for job in jobs],
        }
        if not base["bmc_configured"]:
            return Response(
                {**base, "detail": "BMC endpoint and credential reference must be configured."},
                status=200,
            )
        try:
            credential, trust_bundle_pem = _bmc_credential_for(machine, tenant, project)
            inventory = _firmware_inventory(machine, credential, trust_bundle_pem)
        except (
            ValueError,
            ImportError,
            AttributeError,
            PermissionError,
            RuntimeError,
            httpx.HTTPError,
        ) as exc:
            return _firmware_error_response(exc)
        return Response({**base, **inventory}, status=200)


class FleetMachineFirmwareUpgradeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, machine_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, is_tenant_admin = _tenant_and_project(
            request, tenant_id, project_id
        )
        project_admin = ProjectMembership.objects.filter(
            project=project,
            user=request.user,
            role=ProjectMembership.Role.ADMIN,
        ).exists()
        if not is_tenant_admin and not project_admin:
            return Response(
                {"detail": "Tenant or project administrator role is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        machine = AgentMachine.objects.filter(
            pk=machine_id, tenant=tenant, project=project
        ).first()
        if machine is None:
            return Response({"detail": "Machine not found."}, status=404)
        vendor_gate = _firmware_vendor_gate(machine)
        if vendor_gate is not None:
            return vendor_gate
        allowed = {"image_url", "component", "apply_time", "targets", "confirm", "idempotency_key"}
        if set(request.data) - allowed:
            return Response(
                {"detail": "Unsupported firmware upgrade fields were supplied."},
                status=400,
            )
        if request.data.get("confirm") is not True:
            return Response(
                {"detail": "Explicit confirm=true is required for firmware upgrades."},
                status=400,
            )
        try:
            image_url = _validate_firmware_image_url(request.data.get("image_url"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        component = request.data.get("component", "")
        if component and (not isinstance(component, str) or len(component) > 160):
            return Response({"detail": "component must be at most 160 characters."}, status=400)
        apply_time = request.data.get("apply_time", "Immediate")
        if apply_time not in ("Immediate", "OnReset", "AtMaintenanceWindowStart"):
            return Response({"detail": "apply_time is invalid."}, status=400)
        targets = request.data.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 8:
            return Response(
                {"detail": "targets must be a list of at most 8 Redfish paths."},
                status=400,
            )
        for target in targets:
            if not isinstance(target, str) or not re.fullmatch(
                r"/redfish/v1/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,240}", target
            ):
                return Response({"detail": "targets contains an invalid Redfish path."}, status=400)
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
            return Response(
                {"detail": "Idempotency-Key header is required (8-128 safe characters)."},
                status=400,
            )
        machine = AgentMachine.objects.filter(
            pk=machine_id, tenant=tenant, project=project
        ).first()
        if machine is None:
            return Response({"detail": "Machine not found."}, status=404)
        if not machine.bmc_endpoint or not machine.bmc_credential_ref:
            return Response(
                {"detail": "BMC endpoint and credential reference must be configured."},
                status=409,
            )
        existing = FirmwareUpdateJob.objects.filter(
            tenant=tenant, machine=machine, idempotency_key=idempotency_key
        ).first()
        if existing:
            return Response(_firmware_job_data(existing), status=200)
        try:
            credential, trust_bundle_pem = _bmc_credential_for(machine, tenant, project)
            result = _firmware_simple_update(
                machine,
                credential,
                trust_bundle_pem,
                image_url,
                targets,
                apply_time,
            )
        except (
            ValueError,
            ImportError,
            AttributeError,
            PermissionError,
            RuntimeError,
            httpx.HTTPError,
        ) as exc:
            return _firmware_error_response(exc)
        with transaction.atomic():
            job, created = FirmwareUpdateJob.objects.get_or_create(
                tenant=tenant,
                machine=machine,
                idempotency_key=idempotency_key,
                defaults={
                    "project": project,
                    "requested_by": request.user,
                    "image_url": _sanitize_image_url(image_url),
                    "component": component,
                    "apply_time": apply_time,
                    "targets": targets,
                    "status": FirmwareUpdateJob.Status.SUBMITTED,
                    "task_monitor": result["task_monitor"],
                    "message": result["message"],
                },
            )
            if created:
                record_audit_event(
                    request,
                    tenant,
                    "fleet.machine.firmware_update_requested",
                    "firmware_update_job",
                    job.id,
                    project=project,
                    metadata={
                        "machine_id": str(machine.id),
                        "component": component,
                        "apply_time": apply_time,
                        "target_count": len(targets),
                        "image_host": urlsplit(image_url).netloc,
                    },
                )
        return Response(_firmware_job_data(job), status=202 if created else 200)


class FleetMachineFirmwareUpgradeDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id, machine_id, job_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        machine = AgentMachine.objects.filter(
            pk=machine_id, tenant=tenant, project=project
        ).first()
        if machine is None:
            return Response({"detail": "Machine not found."}, status=404)
        job = FirmwareUpdateJob.objects.filter(
            pk=job_id, machine=machine, tenant=tenant, project=project
        ).first()
        if job is None:
            return Response({"detail": "Firmware update job not found."}, status=404)
        try:
            job = _refresh_firmware_job(job)
        except (
            ValueError,
            ImportError,
            AttributeError,
            PermissionError,
            RuntimeError,
            httpx.HTTPError,
        ) as exc:
            return _firmware_error_response(exc)
        return Response(_firmware_job_data(job), status=200)


class FleetMachineLifecycleReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, machine_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, is_tenant_admin = _tenant_and_project(
            request,
            tenant_id,
            project_id,
        )
        project_editor = ProjectMembership.objects.filter(
            project=project,
            user=request.user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
        if not is_tenant_admin and not project_editor:
            return Response(
                {"detail": "Project editor or administrator role is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        expected_state = request.data.get("expected_state")
        target_state = request.data.get("target_state")
        reason = request.data.get("reason")
        if not isinstance(expected_state, str) or not isinstance(target_state, str):
            return Response(
                {"detail": "expected_state and target_state are required strings."},
                status=400,
            )
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
            return Response(
                {"detail": "reason must contain 10 to 500 characters."},
                status=400,
            )
        transitions = {
            "discovered": {"identity_review"},
            "identity_review": {"discovered"},
        }
        if target_state not in transitions.get(expected_state, set()):
            return Response(
                {
                    "detail": "This lifecycle transition is not enabled.",
                    "code": "LIFECYCLE_TRANSITION_NOT_ENABLED",
                },
                status=409,
            )
        with transaction.atomic():
            machine = AgentMachine.objects.select_for_update().filter(
                pk=machine_id,
                tenant=tenant,
                project=project,
            ).first()
            if machine is None:
                return Response({"detail": "Machine not found."}, status=404)
            if machine.lifecycle_state != expected_state:
                return Response(
                    {
                        "detail": "Machine lifecycle state changed.",
                        "current_state": machine.lifecycle_state,
                    },
                    status=409,
                )
            machine.lifecycle_state = target_state
            machine.save(update_fields=["lifecycle_state", "updated_at"])
            record_audit_event(
                request,
                tenant,
                "fleet.machine.lifecycle_reviewed",
                "agent_machine",
                machine.id,
                project=project,
                metadata={
                    "agent_node_id": str(machine.agent_node_id),
                    "from_state": expected_state,
                    "to_state": target_state,
                    "reason": reason.strip(),
                },
            )
        return Response(
            {
                "machine_id": str(machine.id),
                "lifecycle_state": machine.lifecycle_state,
            }
        )


class AgentRuntimeInventoryReportView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        node = _authenticate_node(request, request.data.get("node_id"))
        if node is None:
            return Response({"detail": "Invalid or revoked node credential."}, status=401)
        inventory = request.data.get("hardware_inventory")
        if not isinstance(inventory, dict):
            return Response({"detail": "hardware_inventory must be a JSON object."}, status=400)
        if _json_size_bytes(inventory) > 262144:
            return Response({"detail": "hardware_inventory must not exceed 256 KiB."}, status=413)
        if inventory.get("status") != "observed":
            return Response(
                {"detail": "Only observed hardware inventory can be ingested."},
                status=400,
            )
        system_identity = inventory.get("system_identity", {})
        if not isinstance(system_identity, dict):
            return Response({"detail": "system_identity must be a JSON object."}, status=400)
        identity_fields = (
            "sys_vendor",
            "product_name",
            "product_uuid",
            "product_serial",
            "board_serial",
            "chassis_serial",
        )
        for key in identity_fields:
            value = system_identity.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 256):
                return Response({"detail": "Invalid system identity field."}, status=400)
        material = _machine_identity_material(system_identity)
        fingerprint_payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
        fingerprint = ""
        if material:
            fingerprint = hashlib.sha256(
                fingerprint_payload.encode("utf-8")
            ).hexdigest()
        observed_at = timezone.now()
        try:
            with transaction.atomic():
                locked_node = AgentNode.objects.select_for_update().get(pk=node.pk)
                if locked_node.status == AgentNode.Status.REVOKED:
                    return Response({"detail": "Invalid or revoked node credential."}, status=401)
                machine, _ = AgentMachine.objects.select_for_update().get_or_create(
                    agent_node=locked_node,
                    defaults={
                        "tenant": locked_node.tenant,
                        "project": locked_node.project,
                    },
                )
                conflicts = _machine_conflicts(locked_node, material, machine)
                for conflict in conflicts:
                    candidate = AgentMachine.objects.select_for_update().get(
                        pk=conflict["machine_id"]
                    )
                    reverse_conflict = {
                        "machine_id": str(machine.id),
                        "agent_node_id": str(locked_node.id),
                        "matching_fields": conflict["matching_fields"],
                    }
                    existing_conflicts = candidate.identity_conflicts or []
                    if reverse_conflict not in existing_conflicts:
                        candidate.identity_conflicts = [
                            *existing_conflicts,
                            reverse_conflict,
                        ]
                    candidate.identity_status = AgentMachine.IdentityStatus.CONFLICT
                    candidate.save(
                        update_fields=[
                            "identity_conflicts",
                            "identity_status",
                            "updated_at",
                        ]
                    )
                if machine.identity_status == AgentMachine.IdentityStatus.VERIFIED:
                    if not material:
                        return Response(
                            {
                                "detail": (
                                    "Verified machine identity cannot be replaced by "
                                    "an inventory report without identity material."
                                )
                            },
                            status=409,
                        )
                    if fingerprint != machine.identity_fingerprint:
                        conflicts.append(
                            {
                                "type": "verified_identity_material_changed",
                                "previous_fingerprint": machine.identity_fingerprint,
                                "observed_fingerprint": fingerprint,
                                "observed_at": observed_at.isoformat(),
                            }
                        )
                machine.vendor = str(system_identity.get("sys_vendor") or "")[:256]
                machine.product_name = str(system_identity.get("product_name") or "")[:256]
                machine.hardware_inventory = inventory
                machine.inventory_source = "host_sysfs_dmi_read_only"
                machine.observed_at = observed_at
                if machine.identity_status == AgentMachine.IdentityStatus.VERIFIED:
                    machine.identity_conflicts = conflicts
                    if conflicts:
                        machine.identity_status = AgentMachine.IdentityStatus.CONFLICT
                    machine.save()
                else:
                    machine.system_uuid = material.get("product_uuid", "")
                    machine.identity_material = material
                    machine.identity_fingerprint = fingerprint
                    machine.identity_conflicts = conflicts
                    machine.identity_status = (
                        AgentMachine.IdentityStatus.CONFLICT
                        if conflicts
                        else AgentMachine.IdentityStatus.CANDIDATE
                        if material
                        else AgentMachine.IdentityStatus.UNVERIFIED
                    )
                    machine.save()
        except IntegrityError:
            return Response(
                {"detail": "Machine inventory update conflicted; retry the report."},
                status=409,
            )
        return Response({
            "machine_id": str(machine.id),
            "agent_node_id": str(locked_node.id),
            "identity_status": machine.identity_status,
            "identity_fingerprint": machine.identity_fingerprint or None,
            "identity_conflicts": machine.identity_conflicts,
            "observed_at": machine.observed_at,
            "attestation": "unverified_agent_report",
        })


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
            _cancel_agent_commands(
                AgentCommand.objects.filter(
                    node=node,
                    status__in=[
                        AgentCommand.Status.QUEUED,
                        AgentCommand.Status.WAITING_APPROVAL,
                        AgentCommand.Status.RUNNING,
                    ],
                ),
                timezone.now(),
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


class AgentNodeCredentialRotateView(APIView):
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
            return Response(
                {"detail": "Tenant owner or admin role is required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if node.status == AgentNode.Status.REVOKED:
                return Response(
                    {"detail": "A revoked agent node cannot rotate credentials."},
                    status=status.HTTP_409_CONFLICT,
                )
            if AgentCommand.objects.filter(
                node=node,
                status=AgentCommand.Status.RUNNING,
            ).exists():
                return Response(
                    {
                        "detail": (
                            "Credential rotation is blocked while commands are running. "
                            "Wait for them to finish or reconcile their execution state."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            raw_token = secrets.token_urlsafe(36)
            node.credential_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            node.status = AgentNode.Status.OFFLINE
            node.last_seen_at = None
            node.last_seen_ip = None
            node.save(
                update_fields=[
                    "credential_hash",
                    "status",
                    "last_seen_at",
                    "last_seen_ip",
                    "updated_at",
                ]
            )

        record_audit_event(
            request,
            node.tenant,
            "agent.node.credential_rotated",
            "agent_node",
            node.id,
            project=node.project,
            metadata={"node_type": node.node_type, "node_name": node.name},
        )
        data = _node_data(node)
        data["enrollment_token"] = raw_token
        data["token_warning"] = (
            "The previous credential is invalid. Store this new token securely; "
            "it is shown only once. The agent remains offline until restarted with it."
        )
        return Response(data)


class AgentNodeClientCertificateView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_admin_node(self, request, node_id):
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
            return None, Response(
                {"detail": "Tenant owner or admin role is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return node, None

    def put(self, request, node_id):
        if not isinstance(request.data, dict) or set(request.data) != {"fingerprint_sha256"}:
            return Response(
                {
                    "detail": (
                        "Only fingerprint_sha256 is accepted; certificate and key "
                        "material are forbidden."
                    )
                },
                status=400,
            )
        fingerprint = request.data.get("fingerprint_sha256")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
            return Response(
                {"detail": "fingerprint_sha256 must be exactly 64 hexadecimal characters."},
                status=400,
            )
        node, error = self._get_admin_node(request, node_id)
        if error:
            return error
        fingerprint = fingerprint.lower()
        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if node.status == AgentNode.Status.REVOKED:
                return Response(
                    {"detail": "A revoked node cannot bind a client certificate."},
                    status=409,
                )
            previous = node.client_certificate_fingerprint
            node.client_certificate_fingerprint = fingerprint
            node.client_certificate_bound_at = timezone.now()
            node.client_certificate_revoked_at = None
            try:
                node.save(update_fields=[
                    "client_certificate_fingerprint",
                    "client_certificate_bound_at",
                    "client_certificate_revoked_at",
                    "updated_at",
                ])
            except IntegrityError:
                return Response(
                    {"detail": "This client certificate is already bound to another agent node."},
                    status=409,
                )
        record_audit_event(
            request,
            node.tenant,
            "agent.node.client_certificate_bound",
            "agent_node",
            node.id,
            project=node.project,
            metadata={
                "previous_fingerprint_sha256": previous or None,
                "fingerprint_sha256": fingerprint,
            },
        )
        return Response(_node_data(node))

    def delete(self, request, node_id):
        if request.data not in ({}, None):
            return Response({"detail": "A request body is not accepted."}, status=400)
        node, error = self._get_admin_node(request, node_id)
        if error:
            return error
        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if not node.client_certificate_fingerprint:
                return Response({"detail": "No client certificate is bound."}, status=409)
            previous = node.client_certificate_fingerprint
            node.client_certificate_fingerprint = ""
            node.client_certificate_revoked_at = timezone.now()
            node.status = AgentNode.Status.OFFLINE
            node.last_seen_at = None
            node.last_seen_ip = None
            node.save(update_fields=[
                "client_certificate_fingerprint",
                "client_certificate_revoked_at",
                "status",
                "last_seen_at",
                "last_seen_ip",
                "updated_at",
            ])
        record_audit_event(
            request,
            node.tenant,
            "agent.node.client_certificate_revoked",
            "agent_node",
            node.id,
            project=node.project,
            metadata={"fingerprint_sha256": previous},
        )
        return Response(_node_data(node))


class FleetNetworkCollectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, is_admin = _tenant_and_project(request, tenant_id, project_id)
        project_editor = ProjectMembership.objects.filter(
            project=project,
            user=request.user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
        if not is_admin and not project_editor:
            return Response({"detail": "Project editor permission required."}, status=403)
        requested_ids = request.data.get("node_ids")
        nodes = AgentNode.objects.filter(
            tenant=tenant,
            project=project,
            status=AgentNode.Status.ONLINE,
            node_type__in=[
                AgentNode.NodeType.EPH_NODE,
                AgentNode.NodeType.TARGET_CLUSTER,
            ],
        ).order_by("name", "id")
        if requested_ids is not None:
            if not isinstance(requested_ids, list) or len(requested_ids) > 500:
                return Response(
                    {"detail": "node_ids must be a list of at most 500 ids."},
                    status=400,
                )
            try:
                parsed = [uuid.UUID(str(item)) for item in requested_ids]
            except (TypeError, ValueError, AttributeError):
                return Response({"detail": "node_ids contains an invalid id."}, status=400)
            nodes = nodes.filter(id__in=parsed)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 96:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)
        plan_hash = _payload_hash("host.network.observe", {})
        queued = []
        skipped = []
        with transaction.atomic():
            for node in nodes:
                key = f"{idempotency_key}:{node.id}"
                command, created = AgentCommand.objects.get_or_create(
                    tenant=tenant,
                    idempotency_key=key,
                    defaults={
                        "project": project,
                        "node": node,
                        "requested_by": request.user,
                        "command_type": "host.network.observe",
                        "payload": {},
                        "status": AgentCommand.Status.QUEUED,
                        "plan_hash": plan_hash,
                    },
                )
                if created:
                    queued.append({"node_id": str(node.id), "command_id": str(command.id)})
                else:
                    skipped.append({"node_id": str(node.id), "reason": "already_queued"})
        if queued:
            record_audit_event(
                request,
                tenant,
                "fleet.network.collect_queued",
                "project",
                project.id,
                project=project,
                metadata={"queued": len(queued), "skipped": len(skipped)},
            )
        return Response(
            {
                "queued_count": len(queued),
                "skipped_count": len(skipped),
                "queued": queued,
                "skipped": skipped,
            },
            status=202,
        )


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
        if command_type == "cluster.capi.worker.provision":
            return Response(
                {
                    "detail": (
                        "Worker provisioning commands can only be queued "
                        "by the deployment workflow."
                    )
                },
                status=403,
            )
        payload = request.data.get("payload", {})
        payload_error = _validate_payload(payload)
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        if command_type == "cluster.kubernetes.version.upgrade":
            if node.node_type != AgentNode.NodeType.TARGET_CLUSTER:
                return Response(
                    {"detail": "Kubernetes version changes require a Target Cluster Agent."},
                    status=400,
                )
            if (
                set(payload)
                != {"cluster_name", "current_version", "target_version", "execution_mode"}
                or payload.get("cluster_name") != node.cluster_name
                or not isinstance(payload.get("current_version"), str)
                or not re.fullmatch(r"v1\.\d+(?:\.\d+)?", payload["current_version"])
                or not isinstance(payload.get("target_version"), str)
                or payload.get("target_version") not in SUPPORTED_KUBERNETES_MINOR_VERSIONS
                or int(payload["target_version"].split(".")[1])
                != int(payload["current_version"].split(".")[1]) + 1
                or payload.get("execution_mode") != "simulation"
            ):
                return Response(
                    {"detail": "Kubernetes version request is invalid or unsupported."},
                    status=400,
                )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)
        plan_hash = _payload_hash(command_type, payload)
        initial_status = (
            AgentCommand.Status.WAITING_APPROVAL
            if command_type in APPROVAL_REQUIRED_COMMANDS
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
        with transaction.atomic():
            command = get_object_or_404(
                AgentCommand.objects.select_for_update().filter(
                    tenant__memberships__user=request.user,
                ),
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
        if _json_size_bytes(request.data) > 16384:
            return Response(
                {"detail": "heartbeat payload must not exceed 16 KiB."},
                status=413,
            )
        capabilities = request.data.get("capabilities", node.capabilities)
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > 64
            or not all(
                isinstance(item, str)
                and 1 <= len(item) <= 96
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", item)
                for item in capabilities
            )
            or len(set(capabilities)) != len(capabilities)
        ):
            return Response(
                {
                    "detail": (
                        "capabilities must contain at most 64 unique "
                        "identifier strings of 1–96 characters."
                    )
                },
                status=400,
            )
        agent_version = request.data.get("agent_version", node.agent_version)
        if (
            not isinstance(agent_version, str)
            or len(agent_version) > 40
            or (agent_version and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", agent_version))
        ):
            return Response({"detail": "agent_version is invalid."}, status=400)
        cluster_name = request.data.get("cluster_name", node.cluster_name)
        if not isinstance(cluster_name, str) or len(cluster_name) > 120:
            return Response({"detail": "cluster_name is invalid."}, status=400)
        with transaction.atomic():
            node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if node.status == AgentNode.Status.REVOKED:
                return Response({"detail": "Invalid or revoked node credential."}, status=401)
            node.agent_version = agent_version
            node.capabilities = capabilities
            node.cluster_name = cluster_name
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
            allowed_commands = (
                EPH_COMMANDS
                if node.node_type == AgentNode.NodeType.EPH_NODE
                else TARGET_COMMANDS
            )
            pending_commands = (
                AgentCommand.objects.select_for_update(skip_locked=True)
                .filter(node=node)
                .filter(
                    Q(status=AgentCommand.Status.QUEUED)
                    | Q(
                        status=AgentCommand.Status.RUNNING,
                        lease_expires_at__lt=now,
                    )
                )
                .exclude(command_type__in=allowed_commands)
                .order_by("created_at")
            )
            for pending_command in pending_commands:
                if _command_is_fenced_by_cluster_ownership(pending_command, node):
                    _fail_fenced_command(pending_command, now)
                    continue
                pending_command.status = AgentCommand.Status.FAILED
                pending_command.error = {
                    "code": "COMMAND_NOT_ALLOWED_FOR_AGENT_ROLE",
                    "retryable": False,
                }
                pending_command.finished_at = now
                pending_command.lease_token = None
                pending_command.lease_expires_at = None
                pending_command.save(
                    update_fields=[
                        "status",
                        "error",
                        "finished_at",
                        "lease_token",
                        "lease_expires_at",
                        "updated_at",
                    ],
                )
            command = None
            candidates = (
                AgentCommand.objects.select_for_update(skip_locked=True)
                .filter(node=node, command_type__in=allowed_commands)
                .filter(
                    Q(status=AgentCommand.Status.QUEUED)
                    | Q(
                        status=AgentCommand.Status.RUNNING,
                        lease_expires_at__lt=now,
                    )
                )
                .order_by("created_at", "id")
            )
            for candidate in candidates:
                if _command_is_fenced_by_cluster_ownership(candidate, node):
                    _fail_fenced_command(candidate, now)
                    continue

                lease_expired_while_running = (
                    candidate.status == AgentCommand.Status.RUNNING
                )
                if (
                    candidate.attempts >= candidate.max_attempts
                    or (
                        lease_expired_while_running
                        and candidate.command_type in MUTATING_COMMANDS
                    )
                ):
                    candidate.status = AgentCommand.Status.FAILED
                    candidate.error = {
                        "code": (
                            "CAPI_PIVOT_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
                            if candidate.command_type == "cluster.capi.pivot.execute"
                            and lease_expired_while_running
                            else "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
                            if candidate.command_type in MUTATING_COMMANDS
                            and lease_expired_while_running
                            else "LEASE_EXPIRED"
                        ),
                        "retryable": False,
                    }
                    candidate.finished_at = now
                    candidate.lease_token = None
                    candidate.lease_expires_at = None
                    candidate.save(
                        update_fields=[
                            "status",
                            "error",
                            "finished_at",
                            "lease_token",
                            "lease_expires_at",
                            "updated_at",
                        ]
                    )
                    continue

                approval_expired = (
                    candidate.approved_at is None
                    or candidate.approved_at
                    < now - datetime.timedelta(minutes=15)
                )
                approval_hash_invalid = (
                    not candidate.approved_plan_hash
                    or not hmac.compare_digest(
                        candidate.approved_plan_hash,
                        _payload_hash(candidate.command_type, candidate.payload),
                    )
                )
                if candidate.command_type in APPROVAL_REQUIRED_COMMANDS and (
                    approval_expired or approval_hash_invalid
                ):
                    candidate.status = AgentCommand.Status.FAILED
                    candidate.error = {
                        "code": (
                            "APPROVAL_EXPIRED"
                            if approval_expired
                            else "APPROVAL_HASH_MISMATCH"
                        ),
                        "retryable": False,
                    }
                    candidate.finished_at = now
                    candidate.lease_token = None
                    candidate.lease_expires_at = None
                    candidate.save(
                        update_fields=[
                            "status",
                            "error",
                            "finished_at",
                            "lease_token",
                            "lease_expires_at",
                            "updated_at",
                        ]
                    )
                    continue

                command = candidate
                break

            if command is None:
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
            if _command_is_fenced_by_cluster_ownership(command, node):
                _fail_fenced_command(command, now)
                return Response(
                    {"detail": "EPH Agent no longer owns this cluster command.",
                     "code": "AGENT_CONTROL_OWNERSHIP_FENCED"},
                    status=409,
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
            command = AgentCommand.objects.select_for_update().filter(
                pk=command_id,
                node=node,
            ).first()
            if command is None:
                return Response({"detail": "Command not found."}, status=404)
            if command.status in {
                AgentCommand.Status.SUCCEEDED,
                AgentCommand.Status.FAILED,
            }:
                exact_replay = (
                    outcome == "succeeded"
                    and command.status == AgentCommand.Status.SUCCEEDED
                    and command.result == result
                    and command.error is None
                ) or (
                    outcome == "failed"
                    and command.status == AgentCommand.Status.FAILED
                    and command.result is None
                    and command.error == error
                )
                if exact_replay:
                    return Response(
                        {
                            "id": str(command.id),
                            "status": command.status,
                            "deployment_id": (
                                str(command.deployment_id) if command.deployment_id else None
                            ),
                        }
                    )
                return Response(
                    {"detail": "Command already has a different terminal result."},
                    status=409,
                )
            if (
                command.status != AgentCommand.Status.RUNNING
                or command.lease_token != lease_token
                or command.lease_expires_at is None
                or command.lease_expires_at < timezone.now()
            ):
                return Response({"detail": "Command lease is invalid or expired."}, status=404)
            if _command_is_fenced_by_cluster_ownership(command, node):
                _fail_fenced_command(command, timezone.now())
                return Response(
                    {"detail": "EPH Agent no longer owns this cluster command.",
                     "code": "AGENT_CONTROL_OWNERSHIP_FENCED"},
                    status=409,
                )
            result_error = _validate_kubernetes_upgrade_result(command, result)
            if outcome == "succeeded" and result_error:
                return Response({"detail": result_error}, status=409)
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
            if (
                command.command_type == "host.network.observe"
                and outcome == "succeeded"
            ):
                machine = AgentMachine.objects.select_for_update().filter(
                    agent_node=node,
                ).first()
                if machine is not None:
                    merged = _network_observation(machine.hardware_inventory, result)
                    if merged is not None:
                        machine.hardware_inventory = merged
                        machine.observed_at = timezone.now()
                        machine.inventory_source = "host_network_observe"
                        machine.inventory_revision = machine.inventory_revision + 1
                        machine.save(
                            update_fields=[
                                "hardware_inventory",
                                "observed_at",
                                "inventory_source",
                                "inventory_revision",
                                "updated_at",
                            ]
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
