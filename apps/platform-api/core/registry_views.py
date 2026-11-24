"""API for the central container image registry catalog."""

import re

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api_errors import legacy_error_response
from core.audit import record_audit_event
from core.models import Project, Tenant, TenantMembership
from core.registry_models import RegistryEvent, RegistryImage

DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
STAGE_ORDER = [
    RegistryImage.Stage.DEV,
    RegistryImage.Stage.STAGING,
    RegistryImage.Stage.PRODUCTION,
]

# The promotion ladder is a rule, not a convention: an image only moves one
# stage at a time and Production demands a signed, scanned digest.
PROMOTION_RULES = {
    RegistryImage.Stage.STAGING: [RegistryImage.Stage.DEV],
    RegistryImage.Stage.PRODUCTION: [RegistryImage.Stage.STAGING],
}


def _error(message: str, code: str, http_status: int = 400):
    return legacy_error_response(message, http_status=http_status, code=code)


def registry_internal_host() -> str:
    return getattr(settings, "PLATFORM_REGISTRY_HOST", "registry.ai-agent-stack.internal")


def _scope(request, tenant_id, project_id):
    tenant = get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(), pk=tenant_id
    )
    projects = Project.objects.filter(tenant=tenant)
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    if not tenant_admin:
        projects = projects.filter(memberships__user=request.user)
    project = get_object_or_404(projects.distinct(), pk=project_id)
    return tenant, project


def _internal_ref(image: RegistryImage) -> str:
    return f"{registry_internal_host()}/{image.name}@{image.digest}"


def _serialize(image: RegistryImage) -> dict:
    return {
        "id": str(image.id),
        "name": image.name,
        "category": image.category,
        "source_repository": image.source_repository,
        "build_ref": image.build_ref,
        "digest": image.digest,
        "internal_ref": _internal_ref(image),
        "size_bytes": image.size_bytes,
        "status": image.status,
        "stage": image.stage,
        "scan_status": image.scan_status,
        "scan_findings": image.scan_findings,
        "sbom_ref": image.sbom_ref,
        "signed": image.signed,
        "promoted_at": image.promoted_at.isoformat() if image.promoted_at else None,
        "created_at": image.created_at.isoformat(),
        "updated_at": image.updated_at.isoformat(),
    }


def _log(image: RegistryImage, action: str, actor=None, **detail):
    RegistryEvent.objects.create(
        image=image, action=action, actor=actor, detail=detail or {}
    )


class RegistryImageListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        _, project = _scope(request, tenant_id, project_id)
        images = RegistryImage.objects.filter(project=project)
        category = request.query_params.get("category")
        if category:
            images = images.filter(category=category)
        stage = request.query_params.get("stage")
        if stage:
            images = images.filter(stage=stage)
        image_status = request.query_params.get("status")
        if image_status:
            images = images.filter(status=image_status)
        return Response({"images": [_serialize(image) for image in images]})

    def post(self, request, tenant_id, project_id):
        tenant, project = _scope(request, tenant_id, project_id)
        payload = request.data or {}
        name = str(payload.get("name") or "").strip().lower()
        digest = str(payload.get("digest") or "").strip().lower()
        category = str(payload.get("category") or "").strip().lower()
        if not name:
            return _error("Image name is required", "REGISTRY_NAME_REQUIRED")
        if category not in RegistryImage.Category.values:
            return _error("Unknown image category", "REGISTRY_CATEGORY_INVALID")
        if not DIGEST_PATTERN.match(digest):
            # A tag can move; a deployment reference cannot.
            return _error(
                "Deployments must reference an sha256 digest", "REGISTRY_DIGEST_REQUIRED"
            )
        if RegistryImage.objects.filter(project=project, name=name, digest=digest).exists():
            return _error("This digest is already registered", "REGISTRY_IMAGE_EXISTS", 409)
        image = RegistryImage.objects.create(
            tenant=tenant,
            project=project,
            created_by=request.user,
            name=name,
            category=category,
            digest=digest,
            source_repository=str(payload.get("source_repository") or "").strip(),
            build_ref=str(payload.get("build_ref") or "").strip(),
            size_bytes=int(payload.get("size_bytes") or 0),
            status=RegistryImage.Status.SCANNING,
        )
        _log(image, RegistryEvent.Action.REGISTERED, request.user, digest=digest)
        record_audit_event(
            request,
            tenant,
            "registry.image.register",
            "registry_image",
            str(image.id),
            project=project,
            metadata={"name": name, "digest": digest},
        )
        return Response(_serialize(image), status=status.HTTP_201_CREATED)


class RegistryImageDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id, image_id):
        _, project = _scope(request, tenant_id, project_id)
        image = get_object_or_404(RegistryImage, project=project, pk=image_id)
        return Response(_serialize(image))


class RegistryImageScanView(APIView):
    """Record a scanner verdict; the platform never invents one."""

    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, image_id):
        _, project = _scope(request, tenant_id, project_id)
        image = get_object_or_404(RegistryImage, project=project, pk=image_id)
        payload = request.data or {}
        scan_status = str(payload.get("scan_status") or "").strip().lower()
        if scan_status not in set(RegistryImage.ScanStatus.values):
            return _error("Unknown scan status", "REGISTRY_SCAN_STATUS_INVALID")
        findings = int(payload.get("findings") or 0)
        image.scan_status = scan_status
        image.scan_findings = findings
        image.sbom_ref = str(payload.get("sbom_ref") or image.sbom_ref).strip()
        if scan_status == RegistryImage.ScanStatus.PASSED:
            image.status = RegistryImage.Status.READY
        else:
            image.status = RegistryImage.Status.BLOCKED
        image.save(
            update_fields=["scan_status", "scan_findings", "sbom_ref", "status", "updated_at"]
        )
        _log(
            image,
            RegistryEvent.Action.SCANNED,
            request.user,
            scan_status=scan_status,
            findings=findings,
        )
        return Response(_serialize(image))


class RegistryImageSignView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, image_id):
        _, project = _scope(request, tenant_id, project_id)
        image = get_object_or_404(RegistryImage, project=project, pk=image_id)
        if image.scan_status != RegistryImage.ScanStatus.PASSED:
            return _error(
                "An image must pass scanning before it can be signed",
                "REGISTRY_SIGN_REQUIRES_SCAN",
                409,
            )
        image.signed = True
        image.save(update_fields=["signed", "updated_at"])
        _log(image, RegistryEvent.Action.SIGNED, request.user)
        return Response(_serialize(image))


class RegistryImagePromoteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, image_id):
        _, project = _scope(request, tenant_id, project_id)
        image = get_object_or_404(RegistryImage, project=project, pk=image_id)
        target = str((request.data or {}).get("stage") or "").strip().lower()
        if target not in STAGE_ORDER:
            return _error("Unknown promotion stage", "REGISTRY_STAGE_INVALID")
        allowed = PROMOTION_RULES.get(target)
        if allowed and image.stage not in allowed:
            return _error(
                "Promotion must follow dev, staging, production",
                "REGISTRY_PROMOTION_ORDER",
                409,
            )
        if image.status != RegistryImage.Status.READY:
            return _error("Only a ready image can be promoted", "REGISTRY_NOT_READY", 409)
        if image.scan_status != RegistryImage.ScanStatus.PASSED:
            return _error(
                "A passed scan is required before promotion", "REGISTRY_SCAN_REQUIRED", 409
            )
        if target == RegistryImage.Stage.PRODUCTION and not image.signed:
            return _error(
                "Production requires a signed image", "REGISTRY_SIGNATURE_REQUIRED", 409
            )
        previous = image.stage
        image.stage = target
        image.promoted_at = timezone.now()
        image.save(update_fields=["stage", "promoted_at", "updated_at"])
        _log(
            image,
            RegistryEvent.Action.PROMOTED,
            request.user,
            **{"from": previous, "to": target},
        )
        record_audit_event(
            request,
            image.tenant,
            "registry.image.promote",
            "registry_image",
            str(image.id),
            project=project,
            metadata={"from": previous, "to": target},
        )
        return Response(_serialize(image))


class RegistryPullPlanView(APIView):
    """What a bootstrap or target cluster is allowed to pull, and from where."""

    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        _, project = _scope(request, tenant_id, project_id)
        images = RegistryImage.objects.filter(
            project=project, status=RegistryImage.Status.READY
        )
        category = request.query_params.get("category")
        if category:
            images = images.filter(category=category)
        return Response(
            {
                "registry_host": registry_internal_host(),
                "digest_pinned": True,
                "images": [
                    {
                        "name": image.name,
                        "category": image.category,
                        "digest": image.digest,
                        "internal_ref": _internal_ref(image),
                        "stage": image.stage,
                    }
                    for image in images
                ],
            }
        )


class RegistrySummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        _, project = _scope(request, tenant_id, project_id)
        images = RegistryImage.objects.filter(project=project)
        by_category = {choice: 0 for choice in RegistryImage.Category.values}
        by_stage = {choice: 0 for choice in RegistryImage.Stage.values}
        by_status = {choice: 0 for choice in RegistryImage.Status.values}
        for image in images:
            by_category[image.category] = by_category.get(image.category, 0) + 1
            by_stage[image.stage] = by_stage.get(image.stage, 0) + 1
            by_status[image.status] = by_status.get(image.status, 0) + 1
        total = images.count()
        return Response(
            {
                "total": total,
                "by_category": by_category,
                "by_stage": by_stage,
                "by_status": by_status,
                "signed": images.filter(signed=True).count(),
                "scan_passed": images.filter(
                    scan_status=RegistryImage.ScanStatus.PASSED
                ).count(),
                "registry_host": registry_internal_host(),
            }
        )


class RegistryImageEventListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id, image_id):
        _, project = _scope(request, tenant_id, project_id)
        image = get_object_or_404(RegistryImage, project=project, pk=image_id)
        events = image.events.select_related("actor")[:50]
        return Response(
            {
                "events": [
                    {
                        "id": str(event.id),
                        "action": event.action,
                        "actor": event.actor.username if event.actor else None,
                        "detail": event.detail,
                        "created_at": event.created_at.isoformat(),
                    }
                    for event in events
                ]
            }
        )
