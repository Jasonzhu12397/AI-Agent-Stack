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

from core.audit import record_audit_event
from core.cluster_deployment_services import handle_deployment_command_result
from core.models import (
    AgentCommand,
    AgentMachine,
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
    "cluster.network.inspect",
    "cluster.provision",
    "target-agent.bootstrap",
    "capi.cluster.observe",
    "metal3.host.inspect",
    "cluster.provider.discover",
    "cluster.manifest.validate",
    "cluster.capi.pivot.source.preflight",
    "cluster.capi.pivot.dry-run",
    "cluster.capi.pivot.execute",
}
TARGET_COMMANDS = {
    "node.health",
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
    "k8s.resource.read",
}
MUTATING_COMMANDS = {
    "cluster.provision",
    "target-agent.bootstrap",
    "cluster.capi.pivot.execute",
    "cluster.capi.worker.provision",
}
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
            data["identity"] = {
                "agent_node_id": str(node.id),
                "name": node.name,
                "cluster_name": node.cluster_name,
            }
            machine = AgentMachine.objects.filter(agent_node=node).first()
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
            return Response(\n                {"detail": "This endpoint does not accept a request body."},\n                status=400,\n            )
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
            return Response(\n                {"detail": "BMC endpoint and credential reference must be configured."},\n                status=409,\n            )
        resolver_path = getattr(settings, "BMC_CREDENTIAL_RESOLVER", "")
        if not resolver_path:
            return Response(
                {
                    "detail": "BMC credential resolver is not configured.",
                    "verification_status": "not_checked",
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
            return Response({"detail": "BMC authentication failed."}, status=502)
        except (ValueError, ImportError, AttributeError):
            return Response({"detail": "BMC verification configuration is invalid."}, status=400)
        except httpx.TimeoutException:
            return Response({"detail": "BMC verification timed out."}, status=504)
        except httpx.HTTPError:
            return Response({"detail": "BMC connection or TLS verification failed."}, status=502)
        except RuntimeError:
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
        content_type = (\n            response.headers.get("content-type", "")\n            .split(";", 1)[0]\n            .strip()\n            .lower()\n        )
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
        raise ValueError(\n            "Redfish verification currently requires a literal BMC IP address."\n        ) from exc
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
    if (\n        not isinstance(username, str)\n        or not username\n        or not isinstance(password, str)\n        or not password\n    ):
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
        | Q(identity_material__product_serial__iexact=system["serial"])\n        | Q(identity_material__product_uuid__iexact=system["uuid"])
    )
    if duplicates.exists():
        return "conflict", ["system_uuid", "product_serial"], "duplicate_machine_identity"
    return "verified", [], "dmi_redfish_match"



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
            if (
                command.attempts >= command.max_attempts
                or (
                    command.command_type == "cluster.capi.pivot.execute"
                    and command.status == AgentCommand.Status.RUNNING
                )
            ):
                command.status = AgentCommand.Status.FAILED
                command.error = {
                    "code": (
                        "CAPI_PIVOT_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
                        if command.command_type == "cluster.capi.pivot.execute"
                        else "LEASE_EXPIRED"
                    ),
                    "retryable": False,
                }
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

    PHASE_TRANSITIONS = {
        "target_agent_bootstrap": {"target_agent_bootstrap", "capi_pivot_preflight"},
        "capi_pivot_preflight": {"capi_pivot_preflight", "capi_pivot_verified"},
        "capi_pivot_verified": {"capi_pivot_verified", "worker_provisioning"},
        "worker_provisioning": {"worker_provisioning", "worker_pool_verified"},
        "worker_pool_verified": {"worker_pool_verified"},
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
            if phase in {"eph_reclaim_pending", "completed"}:
                return Response(
                    {
                        "detail": (
                            "EPH reclaim is fail-closed until the hardware reset, "
                            "sanitization and reclaim executor is implemented."
                        ),
                        "code": "EPH_RECLAIM_EXECUTOR_NOT_ENABLED",
                    },
                    status=409,
                )
            if checkpoint is None:
                if phase != "target_agent_bootstrap":
                    return Response(
                        {"detail": "The first checkpoint must be target_agent_bootstrap."},
                        status=409,
                    )
            else:
                allowed_next_phases = self.PHASE_TRANSITIONS.get(checkpoint.phase, set())
                if phase not in allowed_next_phases:
                    return Response(
                        {
                            "detail": "Checkpoint phase transition is not allowed.",
                            "current_phase": checkpoint.phase,
                            "requested_phase": phase,
                        },
                        status=409,
                    )
            if phase == "capi_pivot_verified":
                dual_agent = (deployment.plan_result or {}).get("dual_agent", {})
                source = (deployment.plan_result or {}).get(
                    "capi_pivot_source_preflight",
                    {},
                )
                source_identity = source.get("source_cluster_identity", {})
                if (
                    dual_agent.get("target_agent_owns_cluster") is not True
                    or dual_agent.get("target_agent_cluster_uid") != source_identity.get("uid")
                ):
                    return Response(
                        {
                            "detail": (
                                "CAPI pivot verification must be accepted by the "
                                "control plane before this checkpoint can advance."
                            ),
                            "code": "TARGET_CAPI_ADOPTION_NOT_VERIFIED",
                        },
                        status=409,
                    )
            if phase == "worker_provisioning":
                dual_agent = (deployment.plan_result or {}).get("dual_agent", {})
                if dual_agent.get("target_agent_owns_cluster") is not True:
                    return Response(
                        {
                            "detail": (
                                "Worker provisioning requires verified Target Agent ownership."
                            ),
                            "code": "TARGET_AGENT_OWNERSHIP_REQUIRED",
                        },
                        status=409,
                    )
            if phase == "worker_pool_verified":
                worker_count = checkpoint_data.get("worker_count")
                ready_worker_count = checkpoint_data.get("ready_worker_count")
                not_ready_worker_names = checkpoint_data.get("not_ready_worker_names")
                expected_worker_count = deployment.spec.get("worker_replicas")
                if (
                    checkpoint is None
                    or checkpoint.phase != "worker_provisioning"
                    or not isinstance(expected_worker_count, int)
                    or isinstance(expected_worker_count, bool)
                    or not isinstance(worker_count, int)
                    or isinstance(worker_count, bool)
                    or worker_count != expected_worker_count
                    or not isinstance(ready_worker_count, int)
                    or isinstance(ready_worker_count, bool)
                    or ready_worker_count != expected_worker_count
                    or not isinstance(not_ready_worker_names, list)
                    or not_ready_worker_names
                ):
                    return Response(
                        {
                            "detail": (
                                "Worker pool verification must match the requested replica "
                                "count and every requested worker must be Ready."
                            ),
                            "code": "WORKER_POOL_NOT_FULLY_READY",
                            "expected_worker_count": expected_worker_count,
                        },
                        status=409,
                    )
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
