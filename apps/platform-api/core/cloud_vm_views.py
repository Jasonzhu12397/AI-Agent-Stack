"""Console API for the operator-configured cloud VM providers."""

from __future__ import annotations

import re

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.cloud_vm import (
    CloudVmError,
    build_provider,
    public_providers,
)
from core.models import (
    CloudVmInstance,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_NETWORKS = 8
POWER_ACTIONS = {"start", "stop", "reboot", "hard-reboot"}


def _scope(request, tenant_id, project_id):
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


def _can_edit(request, tenant, project, is_admin):
    if is_admin:
        return True
    return ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()


def _error(exc: CloudVmError) -> Response:
    return legacy_error_response(exc.message, http_status=exc.http_status, code=exc.code)


def _vm_data(vm: CloudVmInstance) -> dict:
    return {
        "id": str(vm.id),
        "tenant_id": str(vm.tenant_id),
        "project_id": str(vm.project_id),
        "provider_key": vm.provider_key,
        "server_id": vm.server_id,
        "name": vm.name,
        "flavor_ref": vm.flavor_ref,
        "image_ref": vm.image_ref,
        "network_ids": vm.network_ids,
        "status": vm.status,
        "ip_address": vm.ip_address,
        "provider_metadata": vm.provider_metadata,
        "created_at": vm.created_at,
        "updated_at": vm.updated_at,
    }


def _string_field(payload, field, *, pattern, max_length, required=True):
    value = payload.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise CloudVmError("CLOUD_VM_REQUEST_INVALID", f"{field} is required.")
    text = value.strip()
    if len(text) > max_length or not pattern.fullmatch(text):
        raise CloudVmError(
            "CLOUD_VM_REQUEST_INVALID",
            f"{field} must match {pattern.pattern} and be at most {max_length} characters.",
        )
    return text


def _network_ids(payload) -> list[str]:
    value = payload.get("network_ids")
    if not isinstance(value, list) or not value:
        raise CloudVmError("CLOUD_VM_REQUEST_INVALID", "network_ids must be a non-empty list.")
    if len(value) > MAX_NETWORKS:
        raise CloudVmError(
            "CLOUD_VM_REQUEST_INVALID", f"At most {MAX_NETWORKS} networks are allowed."
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CloudVmError("CLOUD_VM_REQUEST_INVALID", "Every network id must be a string.")
        text = item.strip()
        if not text or len(text) > 128:
            raise CloudVmError("CLOUD_VM_REQUEST_INVALID", "A network id is malformed.")
        if text in result:
            raise CloudVmError("CLOUD_VM_REQUEST_INVALID", "network_ids must be unique.")
        result.append(text)
    return result


def _refresh(provider_key: str, rows: list[CloudVmInstance]) -> tuple[list[str], dict[str, dict]]:
    """Reconcile stored rows with what the provider reports.

    A provider outage becomes a warning on the list response instead of a 502:
    operators still need to see the last known state of every VM.
    """

    warnings: list[str] = []
    observed: dict[str, dict] = {}
    if not rows:
        return warnings, observed
    try:
        provider = build_provider(provider_key)
    except CloudVmError as exc:
        warnings.append(exc.message)
        return warnings, observed
    try:
        servers = provider.list_servers()
    except CloudVmError as exc:
        warnings.append(exc.message)
        return warnings, observed
    finally:
        provider.close()
    for server in servers:
        observed[server["server_id"]] = server
    for row in rows:
        snapshot = observed.get(row.server_id)
        if snapshot is None:
            row.status = "UNKNOWN"
            row.save(update_fields=["status", "updated_at"])
            continue
        fields = []
        if row.status != snapshot["status"]:
            row.status = snapshot["status"]
            fields.append("status")
        if snapshot["ip_address"] and row.ip_address != snapshot["ip_address"]:
            row.ip_address = snapshot["ip_address"]
            fields.append("ip_address")
        if snapshot["metadata"]:
            merged = {**row.provider_metadata, **snapshot["metadata"]}
            if merged != row.provider_metadata:
                row.provider_metadata = merged
                fields.append("provider_metadata")
        if fields:
            row.save(update_fields=[*fields, "updated_at"])
    return warnings, observed


class CloudVmProviderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        _scope(request, tenant_id, project_id)
        try:
            providers = public_providers()
        except CloudVmError as exc:
            return _error(exc)
        return Response({"count": len(providers), "results": providers})


class CloudVmListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _is_admin = _scope(request, tenant_id, project_id)
        rows = list(
            CloudVmInstance.objects.filter(tenant=tenant, project=project).order_by("-created_at")[
                :100
            ]
        )
        warnings: list[str] = []
        if request.query_params.get("refresh") in {"1", "true", "yes"}:
            for key in sorted({row.provider_key for row in rows}):
                scoped = [row for row in rows if row.provider_key == key]
                provider_warnings, _observed = _refresh(key, scoped)
                warnings.extend(provider_warnings)
        return Response(
            {
                "count": len(rows),
                "results": [_vm_data(row) for row in rows],
                "warnings": warnings,
            }
        )

    def post(self, request, tenant_id, project_id):
        if not isinstance(request.data, dict):
            return legacy_error_response("Request body must be a JSON object.", http_status=400)
        tenant, project, is_admin = _scope(request, tenant_id, project_id)
        if not _can_edit(request, tenant, project, is_admin):
            return legacy_error_response("Project editor permission required.", http_status=403)
        try:
            provider_key = _string_field(
                request.data,
                "provider",
                pattern=re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$"),
                max_length=64,
            )
            name = _string_field(request.data, "name", pattern=NAME_PATTERN, max_length=63)
            flavor_ref = _string_field(
                request.data, "flavor_ref", pattern=REF_PATTERN, max_length=128
            )
            image_ref = _string_field(
                request.data, "image_ref", pattern=REF_PATTERN, max_length=128
            )
            networks = _network_ids(request.data)
        except CloudVmError as exc:
            return _error(exc)
        try:
            provider = build_provider(provider_key)
        except CloudVmError as exc:
            return _error(exc)
        created_remote = False
        try:
            snapshot = provider.create_server(
                name=name,
                flavor_ref=flavor_ref,
                image_ref=image_ref,
                network_ids=networks,
            )
            created_remote = True
        except CloudVmError as exc:
            return _error(exc)
        finally:
            provider.close()
        try:
            vm = CloudVmInstance.objects.create(
                tenant=tenant,
                project=project,
                created_by=request.user,
                provider_key=provider_key,
                server_id=snapshot["server_id"],
                name=snapshot.get("name") or name,
                flavor_ref=flavor_ref,
                image_ref=image_ref,
                network_ids=networks,
                status=snapshot["status"],
                ip_address=snapshot["ip_address"],
                provider_metadata=snapshot["metadata"],
            )
        except IntegrityError:
            # The provider accepted the server but our unique row already owns
            # that server id. Deleting the fresh server keeps the inventory from
            # drifting; conficts are unlikely because names are user-provided.
            if created_remote:
                _best_effort_delete(provider_key, snapshot["server_id"])
            return legacy_error_response(
                "A cloud VM with this server id is already registered.",
                http_status=409,
            )
        record_audit_event(
            request,
            tenant,
            "cloud_vm.created",
            "cloud_vm",
            vm.id,
            project=project,
            metadata={
                "provider": provider_key,
                "server_id": vm.server_id,
                "name": vm.name,
                "network_count": len(networks),
            },
        )
        return Response(_vm_data(vm), status=201)


class CloudVmDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, vm_id):
        vm = get_object_or_404(CloudVmInstance, pk=vm_id)
        tenant, project, _is_admin = _scope(request, vm.tenant_id, vm.project_id)
        return vm, tenant, project

    def get(self, request, vm_id):
        vm, _tenant, _project = self._get(request, vm_id)
        warnings: list[str] = []
        if request.query_params.get("refresh") in {"1", "true", "yes"}:
            warnings, _observed = _refresh(vm.provider_key, [vm])
        return Response({**_vm_data(vm), "warnings": warnings})

    def delete(self, request, vm_id):
        vm, tenant, project = self._get(request, vm_id)
        is_admin = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not _can_edit(request, tenant, project, is_admin):
            return legacy_error_response("Project editor permission required.", http_status=403)
        try:
            provider = build_provider(vm.provider_key)
        except CloudVmError as exc:
            return _error(exc)
        try:
            provider.delete_server(vm.server_id)
        except CloudVmError as exc:
            if exc.code != "CLOUD_VM_RESOURCE_NOT_FOUND":
                return _error(exc)
        finally:
            provider.close()
        vm.status = "DELETING"
        vm.save(update_fields=["status", "updated_at"])
        record_audit_event(
            request,
            tenant,
            "cloud_vm.deleted",
            "cloud_vm",
            vm.id,
            project=project,
            metadata={"provider": vm.provider_key, "server_id": vm.server_id},
        )
        return Response(_vm_data(vm), status=202)


class CloudVmActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, vm_id):
        vm = get_object_or_404(CloudVmInstance, pk=vm_id)
        tenant, project, _is_admin = _scope(request, vm.tenant_id, vm.project_id)
        is_admin = TenantMembership.objects.filter(
            tenant=tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        if not _can_edit(request, tenant, project, is_admin):
            return legacy_error_response("Project editor permission required.", http_status=403)
        action = request.data.get("action") if isinstance(request.data, dict) else None
        if action not in POWER_ACTIONS:
            return legacy_error_response(
                "action must be one of start, stop, reboot, hard-reboot.",
                http_status=400,
                code="CLOUD_VM_ACTION_INVALID",
            )
        try:
            provider = build_provider(vm.provider_key)
        except CloudVmError as exc:
            return _error(exc)
        try:
            provider.server_action(vm.server_id, action)
        except CloudVmError as exc:
            return _error(exc)
        finally:
            provider.close()
        vm.status = {
            "start": "STARTING",
            "stop": "STOPPING",
            "reboot": "REBOOTING",
            "hard-reboot": "REBOOTING",
        }[action]
        vm.save(update_fields=["status", "updated_at"])
        record_audit_event(
            request,
            tenant,
            "cloud_vm.action",
            "cloud_vm",
            vm.id,
            project=project,
            metadata={"provider": vm.provider_key, "action": action},
        )
        return Response(_vm_data(vm), status=202)


def _best_effort_delete(provider_key: str, server_id: str) -> None:
    try:
        provider = build_provider(provider_key)
    except CloudVmError:
        return
    try:
        provider.delete_server(server_id)
    except CloudVmError:
        pass
    finally:
        provider.close()
