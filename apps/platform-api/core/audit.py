# ruff: noqa: I001
import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent, Tenant


SENSITIVE_METADATA_PARTS = (
    "password",
    "secret",
    "token",
    "private_key",
    "kubeconfig",
    "api_key",
    "access_key",
    "authorization",
    "credential",
)


def _safe_metadata(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, child in value.items():
        normalized = str(key).lower().replace("-", "_")
        if any(part in normalized for part in SENSITIVE_METADATA_PARTS):
            continue
        if isinstance(child, dict):
            result[str(key)[:80]] = _safe_metadata(child)
        elif isinstance(child, list):
            result[str(key)[:80]] = [
                item for item in child[:50]
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
        elif isinstance(child, (str, int, float, bool)) or child is None:
            result[str(key)[:80]] = child
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("Audit metadata exceeds 8 KiB.")
    return result


def record_audit_event(
    request,
    tenant,
    event_type,
    resource_type,
    resource_id="",
    project=None,
    outcome=AuditEvent.Outcome.SUCCESS,
    metadata=None,
):
    safe_metadata = _safe_metadata(metadata or {})
    timestamp = timezone.now()
    event_id = uuid.uuid4()
    actor_id = getattr(request.user, "pk", None) if request is not None else None
    request_id = ""
    remote_addr = None
    user_agent = ""
    if request is not None:
        request_id = str(request.headers.get("X-Request-ID", ""))[:64]
        remote_addr = request.META.get("REMOTE_ADDR")
        user_agent = str(request.headers.get("User-Agent", ""))[:256]

    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        previous = (
            AuditEvent.objects.filter(tenant=tenant)
            .order_by("-created_at", "-id")
            .first()
        )
        previous_hash = previous.event_hash if previous else ""
        canonical = {
            "id": str(event_id),
            "tenant_id": str(tenant.pk),
            "project_id": str(project.pk) if project else None,
            "actor_id": actor_id,
            "event_type": event_type,
            "resource_type": resource_type,
            "resource_id": str(resource_id)[:64],
            "outcome": outcome,
            "request_id": request_id,
            "remote_addr": remote_addr,
            "user_agent": user_agent,
            "metadata": safe_metadata,
            "previous_hash": previous_hash,
            "created_at": timestamp.isoformat(),
        }
        event_hash = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return AuditEvent.objects.create(
            id=event_id,
            tenant=tenant,
            project=project,
            actor_id=actor_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=str(resource_id)[:64],
            outcome=outcome,
            request_id=request_id,
            remote_addr=remote_addr,
            user_agent=user_agent,
            metadata=safe_metadata,
            previous_hash=previous_hash,
            event_hash=event_hash,
            created_at=timestamp,
        )


def verify_audit_chain(tenant):
    previous_hash = ""
    verified_count = 0
    events = AuditEvent.objects.filter(tenant=tenant).order_by("created_at", "id")
    for event in events.iterator(chunk_size=500):
        canonical = {
            "id": str(event.id),
            "tenant_id": str(event.tenant_id),
            "project_id": str(event.project_id) if event.project_id else None,
            "actor_id": event.actor_id,
            "event_type": event.event_type,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "outcome": event.outcome,
            "request_id": event.request_id,
            "remote_addr": event.remote_addr,
            "user_agent": event.user_agent,
            "metadata": event.metadata,
            "previous_hash": previous_hash,
            "created_at": event.created_at.isoformat(),
        }
        expected_hash = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if event.previous_hash != previous_hash or event.event_hash != expected_hash:
            return {
                "valid": False,
                "verified_count": verified_count,
                "invalid_event_id": str(event.id),
                "expected_previous_hash": previous_hash,
                "stored_previous_hash": event.previous_hash,
            }
        previous_hash = event.event_hash
        verified_count += 1
    return {
        "valid": True,
        "verified_count": verified_count,
        "invalid_event_id": None,
        "head_hash": previous_hash,
    }
