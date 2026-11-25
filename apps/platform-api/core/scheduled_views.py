"""Console endpoints for recurring Control Agent turns."""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import provider_accounts, scheduled_service
from core.api_errors import legacy_error_response
from core.models import ModelConfig, Project, Tenant
from core.scheduled_models import ScheduledChatJob

MAX_PROMPT_CHARS = 4000


def _scope(request, tenant_id, project_id):
    tenant = get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(), pk=tenant_id
    )
    project = get_object_or_404(Project, tenant=tenant, pk=project_id)
    return tenant, project


def job_payload(job):
    return {
        "id": str(job.id),
        "name": job.name,
        "prompt": job.prompt,
        "cadence": job.cadence,
        "hour": job.hour,
        "weekday": job.weekday,
        "execution_target": job.execution_target,
        "effort": job.effort,
        "status": job.status,
        "enabled": job.status == ScheduledChatJob.Status.ACTIVE,
        "thread_id": str(job.thread_id) if job.thread_id else None,
        "model_config_id": str(job.model_config_id) if job.model_config_id else None,
        "next_run_at": job.next_run_at.isoformat(),
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_error_code": job.last_error_code,
    }


class ScheduledJobListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project = _scope(request, tenant_id, project_id)
        jobs = ScheduledChatJob.objects.filter(tenant=tenant, project=project)
        return Response({"items": [job_payload(job) for job in jobs]})

    def post(self, request, tenant_id, project_id):
        tenant, project = _scope(request, tenant_id, project_id)
        payload = request.data or {}
        name = str(payload.get("name") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        if not name or not prompt:
            return legacy_error_response(
                "name and prompt are required.", http_status=400, code="INVALID_SCHEDULED_JOB"
            )
        if len(prompt) > MAX_PROMPT_CHARS:
            return legacy_error_response(
                f"prompt must be at most {MAX_PROMPT_CHARS} characters.",
                http_status=400,
                code="INVALID_SCHEDULED_JOB",
            )
        cadence = str(payload.get("cadence") or ScheduledChatJob.Cadence.DAILY)
        if cadence not in ScheduledChatJob.Cadence.values:
            return legacy_error_response(
                "Unknown cadence.", http_status=400, code="INVALID_SCHEDULED_JOB"
            )
        hour = int(payload.get("hour") if payload.get("hour") is not None else 9)
        weekday = int(payload.get("weekday") or 0)
        if not 0 <= hour <= 23 or not 0 <= weekday <= 6:
            return legacy_error_response(
                "hour must be 0-23 and weekday 0-6.", http_status=400, code="INVALID_SCHEDULED_JOB"
            )
        target = str(payload.get("execution_target") or "cloud")
        if target not in ("cloud", "local"):
            return legacy_error_response(
                "execution_target must be cloud or local.",
                http_status=400,
                code="INVALID_SCHEDULED_JOB",
            )
        effort = str(payload.get("effort") or "auto")
        if effort not in ("auto", "low"):
            return legacy_error_response(
                "effort must be auto or low.", http_status=400, code="INVALID_SCHEDULED_JOB"
            )
        model_config = None
        model_id = payload.get("model_config_id")
        if model_id:
            model_config = ModelConfig.objects.filter(
                provider_accounts.model_config_scope_q(tenant, request.user),
                provider__enabled=True,
                enabled=True,
                pk=model_id,
            ).first() if _is_uuid(model_id) else None
            if model_config is None:
                return legacy_error_response(
                    "Unknown model configuration.", http_status=400, code="INVALID_SCHEDULED_JOB"
                )
        job = ScheduledChatJob.objects.create(
            tenant=tenant,
            project=project,
            created_by=request.user,
            name=name[:160],
            prompt=prompt,
            cadence=cadence,
            hour=hour,
            weekday=weekday,
            execution_target=target,
            effort=effort,
            model_config=model_config,
            next_run_at=timezone.now(),
        )
        job.next_run_at = scheduled_service.next_occurrence(job, timezone.now())
        job.save(update_fields=["next_run_at"])
        return Response(job_payload(job), status=status.HTTP_201_CREATED)


class ScheduledJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _job(self, request, tenant_id, project_id, job_id):
        tenant, project = _scope(request, tenant_id, project_id)
        return get_object_or_404(
            ScheduledChatJob.objects.select_related("thread"),
            pk=job_id,
            tenant=tenant,
            project=project,
        )

    def patch(self, request, tenant_id, project_id, job_id):
        job = self._job(request, tenant_id, project_id, job_id)
        payload = request.data or {}
        reschedule = False
        if "name" in payload:
            job.name = str(payload.get("name") or "").strip()[:160] or job.name
        if "prompt" in payload:
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt or len(prompt) > MAX_PROMPT_CHARS:
                return legacy_error_response(
                    "prompt is invalid.", http_status=400, code="INVALID_SCHEDULED_JOB"
                )
            job.prompt = prompt
        if "cadence" in payload:
            cadence = str(payload.get("cadence") or "")
            if cadence not in ScheduledChatJob.Cadence.values:
                return legacy_error_response(
                    "Unknown cadence.", http_status=400, code="INVALID_SCHEDULED_JOB"
                )
            job.cadence = cadence
            reschedule = True
        if "hour" in payload:
            hour = int(payload.get("hour") or 0)
            if not 0 <= hour <= 23:
                return legacy_error_response(
                    "hour must be 0-23.", http_status=400, code="INVALID_SCHEDULED_JOB"
                )
            job.hour = hour
            reschedule = True
        if "weekday" in payload:
            weekday = int(payload.get("weekday") or 0)
            if not 0 <= weekday <= 6:
                return legacy_error_response(
                    "weekday must be 0-6.", http_status=400, code="INVALID_SCHEDULED_JOB"
                )
            job.weekday = weekday
            reschedule = True
        if "enabled" in payload:
            job.status = (
                ScheduledChatJob.Status.ACTIVE
                if bool(payload.get("enabled"))
                else ScheduledChatJob.Status.PAUSED
            )
            reschedule = True
        if "effort" in payload:
            effort = str(payload.get("effort") or "")
            if effort not in ("auto", "low"):
                return legacy_error_response(
                    "effort must be auto or low.", http_status=400, code="INVALID_SCHEDULED_JOB"
                )
            job.effort = effort
        if "execution_target" in payload:
            target = str(payload.get("execution_target") or "")
            if target not in ("cloud", "local"):
                return legacy_error_response(
                    "execution_target must be cloud or local.",
                    http_status=400,
                    code="INVALID_SCHEDULED_JOB",
                )
            job.execution_target = target
        if reschedule:
            job.next_run_at = scheduled_service.next_occurrence(job, timezone.now())
        job.save()
        return Response(job_payload(job))

    def delete(self, request, tenant_id, project_id, job_id):
        job = self._job(request, tenant_id, project_id, job_id)
        job.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ScheduledJobRunNowView(APIView):
    """Run the turn immediately, on the caller's thread, and record the outcome."""

    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, job_id):
        tenant, project = _scope(request, tenant_id, project_id)
        job = get_object_or_404(
            ScheduledChatJob.objects.select_related("thread", "model_config"),
            pk=job_id,
            tenant=tenant,
            project=project,
        )
        outcome = scheduled_service.run_job(job, trigger="manual")
        if outcome["error_code"]:
            return legacy_error_response(
                f"Scheduled turn failed: {outcome['error_code']}",
                http_status=409,
                code=outcome["error_code"],
            )
        return Response(job_payload(job))


def _is_uuid(value):
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
