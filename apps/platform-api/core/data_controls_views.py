# ruff: noqa: I001
import json
import secrets
import uuid
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.data_controls_models import ArchiveAction, DataExportJob, DeletionRequest
from core.knowledge_models import KnowledgeBase
from core.models import (
    AgentDefinition,
    AgentPluginInstallation,
    AgentRun,
    AgentTeam,
    AuditEvent,
    PluginInstallation,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def error_response(code, message, details=None, status=400):
    return Response(
        {
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
        status=status,
    )


def get_tenant_for_user(request, tenant_id):
    return get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )


def require_admin(request, tenant, project=None):
    if TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists():
        return True
    if project and ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role=ProjectMembership.Role.ADMIN,
    ).exists():
        return True
    return False


def _settings_values(tenant):
    """Read the data-controls section owned by the settings-core worker.

    The durable row may not exist in this worktree yet; fall back to the
    documented server-side defaults instead of inventing values on the UI.
    """
    values = {"retention_days": 30, "export_format": "json", "allow_export": True}
    try:
        from core import settings_models  # type: ignore
    except ImportError:
        return values
    model = getattr(settings_models, "SettingValue", None) or getattr(
        settings_models, "SettingsValue", None
    )
    if model is None:
        return values
    row = model.objects.filter(tenant=tenant, section="data-controls").first()
    if row:
        stored = getattr(row, "values", {}) or {}
        for key in values:
            if key in stored:
                values[key] = stored[key]
    return values


# ---------------------------------------------------------------------------
# Data exports
# ---------------------------------------------------------------------------

TASK_EXPORT_FIELDS = [
    "id",
    "project_id",
    "task_type",
    "status",
    "attempts",
    "max_attempts",
    "created_at",
    "started_at",
    "finished_at",
]
RUN_EXPORT_FIELDS = [
    "id",
    "task_id",
    "provider_name",
    "model_name",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "created_at",
    "finished_at",
]
AUDIT_EXPORT_FIELDS = [
    "id",
    "event_type",
    "resource_type",
    "resource_id",
    "outcome",
    "metadata",
    "created_at",
]

_EXPORT_KINDS = ("tasks", "runs", "audit_events")


def _scoped_tasks(tenant, project):
    queryset = Task.objects.filter(tenant=tenant)
    if project is not None:
        queryset = queryset.filter(project=project)
    return queryset


def _task_row(task):
    row = {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "task_type": task.task_type,
        "status": task.status,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }
    return {key: row[key] for key in TASK_EXPORT_FIELDS}


def _run_row(run):
    row = {
        "id": str(run.id),
        "task_id": str(run.task_id),
        "provider_name": run.provider_name,
        "model_name": run.model_name,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "latency_ms": run.latency_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    return {key: row[key] for key in RUN_EXPORT_FIELDS}


def _audit_row(event):
    row = {
        "id": str(event.id),
        "event_type": event.event_type,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "outcome": event.outcome,
        "metadata": event.metadata,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
    return {key: row[key] for key in AUDIT_EXPORT_FIELDS}


def _export_directory():
    media_root = getattr(settings, "MEDIA_ROOT", None)
    base = Path(media_root) if media_root else settings.BASE_DIR / "media"
    return base / "exports"


def _collect_export(tenant, project, scope):
    kinds = scope.get("kinds") or list(_EXPORT_KINDS)
    unknown = set(kinds) - set(_EXPORT_KINDS)
    if unknown:
        raise ValueError(f"Unsupported export kinds: {sorted(unknown)}")

    payload = {"tenant_id": str(tenant.id), "generated_at": timezone.now().isoformat()}
    row_count = 0

    if "tasks" in kinds:
        tasks = [_task_row(task) for task in _scoped_tasks(tenant, project)]
        payload["tasks"] = tasks
        row_count += len(tasks)
    if "runs" in kinds:
        task_ids = list(_scoped_tasks(tenant, project).values_list("id", flat=True))
        runs = [
            _run_row(run)
            for run in AgentRun.objects.filter(task_id__in=task_ids).select_related("task")
        ]
        payload["runs"] = runs
        row_count += len(runs)
    if "audit_events" in kinds:
        events = [
            _audit_row(event)
            for event in AuditEvent.objects.filter(tenant=tenant)
        ]
        payload["audit_events"] = events
        row_count += len(events)
    return payload, row_count


def _write_export_file(tenant, payload):
    directory = _export_directory() / str(tenant.id)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}_{secrets.token_hex(8)}.json"
    file_path = directory / filename
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return str(file_path)


def _generate_export(job):
    tenant = job.tenant
    project = job.project
    try:
        payload, row_count = _collect_export(tenant, project, job.scope)
        file_ref = _write_export_file(tenant, payload)
        job.row_count = row_count
        job.file_ref = file_ref
        job.status = DataExportJob.Status.SUCCEEDED
        job.error_code = ""
        job.save(update_fields=["row_count", "file_ref", "status", "error_code", "updated_at"])
    except Exception as exc:  # noqa: BLE001 - persisted as failed job with code
        job.status = DataExportJob.Status.FAILED
        job.error_code = "EXPORT_GENERATION_FAILED"
        job.save(update_fields=["status", "error_code", "updated_at"])
        raise exc


def _export_job_payload(job, *, include_file_ref):
    payload = {
        "id": str(job.id),
        "status": job.status,
        "scope": job.scope,
        "row_count": job.row_count,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "error_code": job.error_code,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
    if include_file_ref and job.status == DataExportJob.Status.SUCCEEDED:
        payload["file_ref"] = job.file_ref
    return payload


class DataExportListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_tenant(self):
        return get_tenant_for_user(self.request, self.kwargs["tenant_id"])

    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        jobs = DataExportJob.objects.filter(tenant=tenant)
        return Response(
            {"results": [_export_job_payload(job, include_file_ref=False) for job in jobs]}
        )

    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        body = request.data if isinstance(request.data, dict) else {}
        idempotency_key = str(body.get("idempotency_key") or "").strip()
        if not idempotency_key:
            return error_response(
                "EXPORT_KEY_REQUIRED", "An idempotency_key is required.", status=400
            )
        scope = body.get("scope") or {}
        if not isinstance(scope, dict):
            return error_response(
                "EXPORT_SCOPE_INVALID", "scope must be a JSON object.", status=400
            )

        settings_values = _settings_values(tenant)
        if settings_values.get("allow_export") is False:
            return error_response(
                "EXPORT_DISABLED", "Data export is disabled for this tenant.", status=403
            )

        existing = DataExportJob.objects.filter(
            tenant=tenant, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return Response(_export_job_payload(existing, include_file_ref=True))

        project = None
        project_id = scope.get("project_id")
        if project_id:
            project = get_object_or_404(
                Project.objects.filter(tenant=tenant), pk=project_id
            )

        job = DataExportJob.objects.create(
            tenant=tenant,
            project=project,
            requested_by=request.user,
            scope=scope,
            status=DataExportJob.Status.PENDING,
            idempotency_key=idempotency_key,
        )
        retention_days = int(settings_values.get("retention_days") or 30)
        job.expires_at = timezone.now() + timezone.timedelta(days=retention_days)
        job.save(update_fields=["expires_at", "updated_at"])

        _generate_export(job)
        record_audit_event(
            request,
            tenant,
            "data.export.generated",
            "data_export_job",
            job.id,
            project=project,
            metadata={"row_count": job.row_count, "kinds": scope.get("kinds")},
        )
        return Response(_export_job_payload(job, include_file_ref=True), status=201)


class DataExportDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_tenant(self):
        return get_tenant_for_user(self.request, self.kwargs["tenant_id"])

    def _get_job(self, tenant, job_id):
        return get_object_or_404(DataExportJob.objects.filter(tenant=tenant), pk=job_id)

    def get(self, request, tenant_id, job_id):
        tenant = self.get_tenant()
        job = self._get_job(tenant, job_id)
        if job.status != DataExportJob.Status.SUCCEEDED:
            return error_response(
                "EXPORT_NOT_READY",
                "Export content is not available until the job succeeds.",
                status=409,
            )
        return Response(_export_job_payload(job, include_file_ref=True))


class DataExportExpireView(APIView):
    permission_classes = [IsAuthenticated]

    def get_tenant(self):
        return get_tenant_for_user(self.request, self.kwargs["tenant_id"])

    def post(self, request, tenant_id, job_id):
        tenant = self.get_tenant()
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        job = get_object_or_404(DataExportJob.objects.filter(tenant=tenant), pk=job_id)
        if job.status != DataExportJob.Status.EXPIRED:
            job.status = DataExportJob.Status.EXPIRED
            job.save(update_fields=["status", "updated_at"])
            record_audit_event(
                request,
                tenant,
                "data.export.expired",
                "data_export_job",
                job.id,
                metadata={"row_count": job.row_count},
            )
        return Response(_export_job_payload(job, include_file_ref=False))


# ---------------------------------------------------------------------------
# Archived content aggregation
# ---------------------------------------------------------------------------

ARCHIVE_REGISTRY = {
    "agent": {"model": AgentDefinition, "name": "agent"},
    "agent_team": {"model": AgentTeam, "name": "agent_team"},
    "knowledge_base": {"model": KnowledgeBase, "name": "knowledge_base"},
}


def _registry_entry(item_type):
    return ARCHIVE_REGISTRY.get(item_type)


def _archive_item_payload(item_type, item):
    return {
        "item_type": item_type,
        "item_id": str(item.id),
        "project_id": str(item.project_id),
        "name": item.name,
        "slug": item.slug,
        "description": getattr(item, "description", "") or "",
        "archived_at": item.updated_at.isoformat() if item.updated_at else None,
    }


class ArchiveItemListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = get_tenant_for_user(request, tenant_id)
        project = get_object_or_404(Project.objects.filter(tenant=tenant), pk=project_id)

        requested_type = request.query_params.get("type") or ""
        q = (request.query_params.get("q") or "").strip()
        page = max(1, int(request.query_params.get("page") or 1))
        page_size = 50

        if requested_type and requested_type not in ARCHIVE_REGISTRY:
            return error_response(
                "ARCHIVE_ITEM_TYPE_UNSUPPORTED",
                f"Unknown archive item type: {requested_type}",
                status=400,
            )

        types = [requested_type] if requested_type else list(ARCHIVE_REGISTRY)
        results = []
        for item_type in types:
            model = ARCHIVE_REGISTRY[item_type]["model"]
            queryset = model.objects.filter(
                tenant=tenant, project=project, is_archived=True
            )
            if q:
                queryset = queryset.filter(name__icontains=q)
            for item in queryset:
                results.append(_archive_item_payload(item_type, item))

        results.sort(key=lambda item: item["name"])
        start = (page - 1) * page_size
        return Response(
            {
                "count": len(results),
                "page": page,
                "results": results[start : start + page_size],
            }
        )


class ArchiveItemRestoreView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, item_type, item_id):
        tenant = get_tenant_for_user(request, tenant_id)
        project = get_object_or_404(Project.objects.filter(tenant=tenant), pk=project_id)
        if not require_admin(request, tenant, project):
            return error_response(
                "FORBIDDEN", "Project admin permission is required.", status=403
            )
        entry = _registry_entry(item_type)
        if entry is None:
            return error_response(
                "ARCHIVE_ITEM_TYPE_UNSUPPORTED",
                f"Unknown archive item type: {item_type}",
                status=400,
            )
        item = get_object_or_404(
            entry["model"].objects.filter(tenant=tenant, project=project),
            pk=item_id,
        )
        if not item.is_archived:
            return Response(
                {
                    "already_restored": True,
                    "item": _archive_item_payload(item_type, item),
                }
            )

        with transaction.atomic():
            item.is_archived = False
            item.save(update_fields=["is_archived", "updated_at"])
            ArchiveAction.objects.get_or_create(
                tenant=tenant,
                project=project,
                item_type=item_type,
                item_id=str(item.id),
                action=ArchiveAction.Action.RESTORE,
                defaults={"actor": request.user, "idempotency_key": str(uuid.uuid4())},
            )
        record_audit_event(
            request,
            tenant,
            "archive.restore",
            item_type,
            str(item.id),
            project=project,
            metadata={"name": item.name},
        )
        return Response({"already_restored": False, "item": _archive_item_payload(item_type, item)})


class ArchiveItemPurgeView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, tenant_id, project_id, item_type, item_id):
        tenant = get_tenant_for_user(request, tenant_id)
        project = get_object_or_404(Project.objects.filter(tenant=tenant), pk=project_id)
        if not require_admin(request, tenant, project):
            return error_response(
                "FORBIDDEN", "Project admin permission is required.", status=403
            )
        entry = _registry_entry(item_type)
        if entry is None:
            return error_response(
                "ARCHIVE_ITEM_TYPE_UNSUPPORTED",
                f"Unknown archive item type: {item_type}",
                status=400,
            )
        model = entry["model"]
        item = model.objects.filter(tenant=tenant, project=project, pk=item_id).first()
        if item is None or not item.is_archived:
            return error_response(
                "ARCHIVE_ITEM_NOT_FOUND",
                "Archived item not found in this project.",
                status=404,
            )

        with transaction.atomic():
            ArchiveAction.objects.get_or_create(
                tenant=tenant,
                project=project,
                item_type=item_type,
                item_id=str(item.id),
                action=ArchiveAction.Action.PURGE,
                defaults={"actor": request.user, "idempotency_key": str(uuid.uuid4())},
            )
            item.delete()
        record_audit_event(
            request,
            tenant,
            "archive.purge",
            item_type,
            str(item_id),
            project=project,
            metadata={"name": item.name},
        )
        return Response(status=204)


# ---------------------------------------------------------------------------
# Privacy grants, sessions and deletion requests
# ---------------------------------------------------------------------------


def _grant_payload(installation, source, scopes):
    return {
        "id": str(installation.id),
        "source": source,
        "installation_id": str(installation.id),
        "connector_name": installation.plugin.name,
        "connector_slug": installation.plugin.plugin_key,
        "project_id": str(installation.project_id),
        "scopes": scopes,
        "status": installation.status,
        "granted_at": installation.created_at.isoformat()
        if installation.created_at
        else None,
    }


class PrivacyGrantListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        tenant = get_tenant_for_user(request, tenant_id)
        from core.models import PluginInstallation

        grants = []
        for installation in PluginInstallation.objects.filter(
            tenant=tenant
        ).select_related("plugin", "project"):
            grants.append(
                _grant_payload(installation, "connector", installation.granted_permissions)
            )
        for installation in AgentPluginInstallation.objects.filter(
            tenant=tenant
        ).select_related("plugin", "project"):
            grants.append(
                _grant_payload(
                    installation, "agent_connector", installation.granted_capabilities
                )
            )
        return Response({"results": grants})


class PrivacyGrantRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def _resolve(self, tenant, grant_id):
        from core.models import PluginInstallation

        plugin_installation = PluginInstallation.objects.filter(
            tenant=tenant, pk=grant_id
        ).first()
        if plugin_installation is not None:
            return "plugin", plugin_installation
        agent_installation = AgentPluginInstallation.objects.filter(
            tenant=tenant, pk=grant_id
        ).first()
        if agent_installation is not None:
            return "agent", agent_installation
        return None, None

    def delete(self, request, tenant_id, grant_id):
        tenant = get_tenant_for_user(request, tenant_id)
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        source, installation = self._resolve(tenant, grant_id)
        if installation is None:
            return error_response(
                "GRANT_NOT_FOUND", "Grant not found for this tenant.", status=404
            )

        already_revoked = installation.status == PluginInstallation.Status.DISABLED
        if not already_revoked:
            installation.status = PluginInstallation.Status.DISABLED
            installation.save(update_fields=["status", "updated_at"])
            record_audit_event(
                request,
                tenant,
                "privacy.grant.revoked",
                "connector_installation",
                str(installation.id),
                project=installation.project,
                metadata={"source": source, "connector_slug": installation.plugin.plugin_key},
            )
        return Response({"revoked": True, "already_revoked": already_revoked})


class PrivacySessionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id):
        get_tenant_for_user(request, tenant_id)
        return error_response(
            "PRIVACY_DEPENDENCY_UNAVAILABLE",
            "Session/device details are provided by the account service, which is "
            "not merged in this environment yet.",
            status=503,
        )


class DeletionRequestListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_tenant(self):
        return get_tenant_for_user(self.request, self.kwargs["tenant_id"])

    def get(self, request, tenant_id):
        tenant = self.get_tenant()
        requests = DeletionRequest.objects.filter(tenant=tenant)
        return Response(
            {
                "results": [
                    {
                        "id": str(item.id),
                        "scope": item.scope.get("scope")
                        if isinstance(item.scope, dict)
                        else item.scope,
                        "status": item.status,
                        "audit_ref": item.audit_ref,
                        "executed_at": item.executed_at.isoformat()
                        if item.executed_at
                        else None,
                        "created_at": item.created_at.isoformat()
                        if item.created_at
                        else None,
                    }
                    for item in requests
                ]
            }
        )

    def post(self, request, tenant_id):
        tenant = self.get_tenant()
        if not require_admin(request, tenant):
            return error_response(
                "FORBIDDEN", "Tenant admin permission is required.", status=403
            )
        body = request.data if isinstance(request.data, dict) else {}

        scope_value = body.get("scope")
        if scope_value not in DeletionRequest.Scope.values:
            return error_response(
                "DELETION_SCOPE_UNSUPPORTED",
                "scope must be one of: archived_content, exports.",
                details={"scope": [str(scope_value)]},
                status=400,
            )
        if body.get("confirm") is not True:
            return error_response(
                "DELETION_CONFIRMATION_REQUIRED",
                "confirm must be true to request deletion.",
                status=400,
            )
        idempotency_key = str(body.get("idempotency_key") or "").strip()
        if not idempotency_key:
            return error_response(
                "DELETION_REQUEST_KEY_REQUIRED",
                "An idempotency_key is required.",
                status=400,
            )

        existing = DeletionRequest.objects.filter(
            tenant=tenant, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return Response(
                {
                    "id": str(existing.id),
                    "scope": existing.scope,
                    "status": existing.status,
                    "audit_ref": existing.audit_ref,
                    "executed_at": existing.executed_at.isoformat()
                    if existing.executed_at
                    else None,
                }
            )

        deletion = DeletionRequest(
            tenant=tenant,
            requested_by=request.user,
            scope={"scope": scope_value},
            status=DeletionRequest.Status.REQUESTED,
            idempotency_key=idempotency_key,
        )
        event = record_audit_event(
            request,
            tenant,
            "privacy.deletion_request.created",
            "deletion_request",
            "",
            metadata={"scope": scope_value},
        )
        deletion.audit_ref = str(event.id)
        try:
            deletion.save()
        except IntegrityError:
            existing = DeletionRequest.objects.filter(
                tenant=tenant, idempotency_key=idempotency_key
            ).first()
            return Response(
                {
                    "id": str(existing.id),
                    "scope": existing.scope,
                    "status": existing.status,
                    "audit_ref": existing.audit_ref,
                    "executed_at": existing.executed_at.isoformat()
                    if existing.executed_at
                    else None,
                }
            )
        return Response(
            {
                "id": str(deletion.id),
                "scope": deletion.scope["scope"],
                "status": deletion.status,
                "audit_ref": deletion.audit_ref,
                "executed_at": None,
            },
            status=201,
        )
