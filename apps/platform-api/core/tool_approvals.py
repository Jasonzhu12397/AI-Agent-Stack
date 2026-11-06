# ruff: noqa: I001
import hashlib
import json
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.models import AgentRun, Task, ToolApprovalRequest


MAX_APPROVAL_CHECKPOINT_BYTES = 65536
MAX_APPROVAL_LIFETIME_MINUTES = 60


def canonical_tool_arguments(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object")
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Tool arguments must contain finite JSON values") from exc
    return encoded


def tool_arguments_hash(arguments):
    canonical = canonical_tool_arguments(arguments)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_matches_call(approval, *, task, tool, installation, connection, arguments):
    if approval.task_id != task.id:
        return False
    if approval.tenant_id != task.tenant_id or approval.project_id != task.project_id:
        return False
    if approval.tool_id != tool.id or approval.installation_id != installation.id:
        return False
    if approval.connection_id != getattr(connection, "id", None):
        return False
    if approval.plugin_version != tool.plugin.version:
        return False
    try:
        return approval.arguments_hash == tool_arguments_hash(arguments)
    except ValueError:
        return False


def validate_execution_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict):
        raise ValueError("Execution checkpoint must be a JSON object")
    try:
        encoded = json.dumps(
            checkpoint,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Execution checkpoint must contain finite JSON values") from exc
    if len(encoded) > MAX_APPROVAL_CHECKPOINT_BYTES:
        raise ValueError("Execution checkpoint exceeds 64 KiB")
    return checkpoint


def create_pending_tool_approval(
    *,
    task_id,
    tool,
    installation,
    connection,
    call_id,
    arguments,
    execution_checkpoint,
    lifetime_minutes=15,
):
    if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
        raise ValueError("Tool call ID is invalid")
    if (
        not isinstance(lifetime_minutes, int)
        or isinstance(lifetime_minutes, bool)
        or not 1 <= lifetime_minutes <= MAX_APPROVAL_LIFETIME_MINUTES
    ):
        raise ValueError("Approval lifetime must be between 1 and 60 minutes")
    canonical_arguments = canonical_tool_arguments(arguments)
    safe_checkpoint = validate_execution_checkpoint(execution_checkpoint)
    arguments_hash = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()
    now = timezone.now()

    with transaction.atomic():
        task = Task.objects.select_for_update().select_related("tenant", "project").get(
            pk=task_id
        )
        if task.status != Task.Status.RUNNING:
            raise ValueError("Only a running task can be suspended for approval")
        if task.tenant_id != installation.tenant_id or task.project_id != installation.project_id:
            raise ValueError("Connector installation is outside the task scope")
        if installation.plugin_id != tool.plugin_id:
            raise ValueError("Connector tool does not belong to the installation plugin")
        if installation.plugin_version != tool.plugin.version:
            raise ValueError("Connector installation version is stale")
        if connection is not None and connection.installation_id != installation.id:
            raise ValueError("Connector connection is outside the installation scope")

        approval, created = ToolApprovalRequest.objects.get_or_create(
            task=task,
            call_id=call_id,
            defaults={
                "tenant": task.tenant,
                "project": task.project,
                "tool": tool,
                "installation": installation,
                "connection": connection,
                "arguments": json.loads(canonical_arguments),
                "arguments_hash": arguments_hash,
                "plugin_version": tool.plugin.version,
                "execution_checkpoint": safe_checkpoint,
                "status": ToolApprovalRequest.Status.PENDING,
                "expires_at": now + timedelta(minutes=lifetime_minutes),
            },
        )
        if not created:
            if (
                approval.tenant_id != task.tenant_id
                or approval.project_id != task.project_id
                or approval.tool_id != tool.id
                or approval.installation_id != installation.id
                or approval.connection_id != getattr(connection, "id", None)
                or approval.plugin_version != tool.plugin.version
                or approval.arguments_hash != arguments_hash
            ):
                raise ValueError("Tool call ID was reused with different approval data")
            if approval.status != ToolApprovalRequest.Status.PENDING:
                raise ValueError("Existing tool approval is no longer pending")
            if approval.execution_checkpoint != safe_checkpoint:
                raise ValueError("Existing tool approval checkpoint does not match")
        task.status = Task.Status.WAITING_APPROVAL
        task.lease_owner = None
        task.lease_token = None
        task.lease_expires_at = None
        task.save(
            update_fields=[
                "status",
                "lease_owner",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        return approval


def consume_approved_tool_call(
    *,
    task,
    tool,
    installation,
    connection,
    call_id,
    arguments,
):
    with transaction.atomic():
        approval = (
            ToolApprovalRequest.objects.select_for_update()
            .select_related("task", "tenant", "project", "tool__plugin", "installation")
            .filter(task=task, call_id=call_id)
            .first()
        )
        if approval is None or approval.status != ToolApprovalRequest.Status.APPROVED:
            return False
        locked_task = Task.objects.select_for_update().get(pk=task.pk)
        if locked_task.status != Task.Status.RUNNING:
            raise ValueError("Approved tool call task is not running")
        if approval.expires_at <= timezone.now():
            approval.status = ToolApprovalRequest.Status.EXPIRED
            approval.save(update_fields=["status"])
            raise ValueError("Tool approval has expired")
        if not approval_matches_call(
            approval,
            task=locked_task,
            tool=tool,
            installation=installation,
            connection=connection,
            arguments=arguments,
        ):
            raise ValueError("Approved tool call no longer matches its original request")
        approval.status = ToolApprovalRequest.Status.CONSUMED
        approval.consumed_at = timezone.now()
        approval.save(update_fields=["status", "consumed_at"])
        return True


def expire_pending_tool_approvals(limit=100):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Approval expiry batch size must be between 1 and 1000")
    expired_count = 0
    now = timezone.now()
    for _ in range(limit):
        with transaction.atomic():
            approval = (
                ToolApprovalRequest.objects.select_for_update(skip_locked=True)
                .filter(
                    status=ToolApprovalRequest.Status.PENDING,
                    expires_at__lte=now,
                )
                .order_by("expires_at")
                .first()
            )
            if approval is None:
                break
            task = Task.objects.select_for_update().get(pk=approval.task_id)
            approval.status = ToolApprovalRequest.Status.EXPIRED
            approval.save(update_fields=["status"])
            if task.status == Task.Status.WAITING_APPROVAL:
                task.status = Task.Status.FAILED
                task.finished_at = now
                task.error = {
                    "code": "TOOL_APPROVAL_EXPIRED",
                    "message": "The high-risk tool approval request expired.",
                }
                task.save(
                    update_fields=["status", "finished_at", "error", "updated_at"]
                )
                ToolApprovalRequest.objects.filter(
                    task=task,
                    status=ToolApprovalRequest.Status.PENDING,
                ).exclude(pk=approval.pk).update(
                    status=ToolApprovalRequest.Status.CANCELLED
                )
                AgentRun.objects.filter(task=task, finished_at__isnull=True).update(
                    finished_at=now
                )
            expired_count += 1
    return expired_count
