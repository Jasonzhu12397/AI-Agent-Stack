# ruff: noqa: I001
import hashlib
import hmac
import io
import json
import secrets
import uuid
import zipfile
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import record_audit_event
from core.eph_seed import EPHSeedError, render_nocloud_seed
from core.models import (
    AgentNode,
    EPHSeedArtifact,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)


def _seed_scope(request, tenant_id, project_id):
    tenant = get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
    )
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    projects = Project.objects.filter(tenant=tenant)
    if not tenant_admin:
        projects = projects.filter(memberships__user=request.user)
    project = get_object_or_404(projects.distinct(), pk=project_id)
    project_admin = tenant_admin or ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role=ProjectMembership.Role.ADMIN,
    ).exists()
    return tenant, project, project_admin


def _fernet():
    key = getattr(settings, "EPH_SEED_ENCRYPTION_KEY", "")
    if not key:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError, UnicodeEncodeError):
        return None


def _json_seed_archive(seed):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in ("user-data", "meta-data", "network-config"):
            archive.writestr(filename, seed[filename])
    return output.getvalue()


class EPHSeedArtifactCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, node_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        allowed = {
            "enrollment_token",
            "control_url",
            "agent_image",
            "hostname",
            "network_config",
            "ca_certificate",
            "expires_in_seconds",
        }
        if set(request.data) - allowed:
            return Response({"detail": "Request contains unsupported fields."}, status=400)
        tenant, project, is_admin = _seed_scope(request, tenant_id, project_id)
        if not is_admin:
            return Response({"detail": "Project administrator role is required."}, status=403)
        node = get_object_or_404(
            AgentNode.objects.filter(tenant=tenant, project=project),
            pk=node_id,
            node_type=AgentNode.NodeType.EPH_NODE,
        )
        enrollment_token = request.data.get("enrollment_token")
        if not isinstance(enrollment_token, str) or not 32 <= len(enrollment_token) <= 512:
            return Response(
                {"detail": "A valid one-time enrollment token is required."},
                status=400,
            )
        supplied_hash = hashlib.sha256(enrollment_token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied_hash, node.credential_hash):
            return Response({"detail": "Invalid or already consumed enrollment token."}, status=401)
        try:
            lifetime = int(request.data.get("expires_in_seconds", 1800))
        except (TypeError, ValueError):
            return Response({"detail": "expires_in_seconds must be an integer."}, status=400)
        if not 60 <= lifetime <= 3600:
            return Response(
                {"detail": "Seed lifetime must be between 60 and 3600 seconds."},
                status=400,
            )

        seed_config = {
            "control_url": request.data.get("control_url"),
            "node_id": str(node.id),
            "enrollment_token": enrollment_token,
            "agent_image": request.data.get("agent_image"),
            "hostname": request.data.get("hostname", node.name),
            "network_config": request.data.get("network_config"),
            "ca_certificate": request.data.get("ca_certificate"),
        }
        try:
            seed = render_nocloud_seed(seed_config)
        except EPHSeedError as error:
            return Response({"detail": str(error)}, status=400)

        cipher = _fernet()
        if cipher is None:
            return Response(
                {"detail": "EPH seed encryption is not configured; artifact creation is disabled."},
                status=503,
            )

        now = timezone.now()
        download_token = secrets.token_urlsafe(48)
        encrypted_payload = cipher.encrypt(
            json.dumps(seed, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).decode("ascii")
        with transaction.atomic():
            locked_node = AgentNode.objects.select_for_update().get(pk=node.pk)
            if (
                locked_node.status == AgentNode.Status.REVOKED
                or locked_node.revoked_at is not None
                or not hmac.compare_digest(
                    hashlib.sha256(enrollment_token.encode("utf-8")).hexdigest(),
                    locked_node.credential_hash,
                )
            ):
                return Response({"detail": "Invalid or revoked EPH enrollment token."}, status=401)
            pending = EPHSeedArtifact.objects.select_for_update().filter(
                agent_node=locked_node,
                downloaded_at__isnull=True,
                revoked_at__isnull=True,
                expires_at__gt=now,
            ).exists()
            downloaded = EPHSeedArtifact.objects.select_for_update().filter(
                agent_node=locked_node,
                downloaded_at__isnull=False,
                revoked_at__isnull=True,
            ).exists()
            if pending or downloaded:
                return Response(
                    {
                        "detail": (
                            "An active seed artifact already exists or the enrollment "
                            "token has already been used to download one."
                        )
                    },
                    status=409,
                )
            artifact = EPHSeedArtifact.objects.create(
                agent_node=locked_node,
                created_by=request.user,
                encrypted_payload=encrypted_payload,
                download_token_hash=hashlib.sha256(
                    download_token.encode("utf-8")
                ).hexdigest(),
                expires_at=now + timedelta(seconds=lifetime),
            )
            record_audit_event(
                request,
                tenant,
                "agent.node.eph_seed_created",
                "eph_seed_artifact",
                artifact.id,
                project=project,
                metadata={
                    "node_id": str(node.id),
                    "expires_at": artifact.expires_at.isoformat(),
                    "artifact_id": str(artifact.id),
                },
            )

        return Response(
            {
                "artifact_id": str(artifact.id),
                "node_id": str(node.id),
                "download_url": (
                    "/api/v1/agent-runtime/seed-artifacts/"
                    + str(artifact.id)
                    + "/download/"
                ),
                "download_token": download_token,
                "expires_at": artifact.expires_at,
                "download_once": True,
            },
            status=201,
        )


class EPHSeedArtifactDownloadView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, artifact_id):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 512:
            return Response({"detail": "Invalid seed download credential."}, status=401)
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cipher = _fernet()
        if cipher is None:
            return Response({"detail": "Seed artifact encryption is unavailable."}, status=503)

        with transaction.atomic():
            artifact = EPHSeedArtifact.objects.select_for_update().filter(
                pk=artifact_id
            ).first()
            now = timezone.now()
            if (
                artifact is None
                or artifact.revoked_at is not None
                or artifact.downloaded_at is not None
                or artifact.expires_at <= now
                or artifact.agent_node.status == AgentNode.Status.REVOKED
                or artifact.agent_node.revoked_at is not None
                or not hmac.compare_digest(supplied_hash, artifact.download_token_hash)
            ):
                return Response(
                    {"detail": "Seed artifact is unavailable, expired, revoked, or consumed."},
                    status=404,
                )
            try:
                seed = json.loads(
                    cipher.decrypt(artifact.encrypted_payload.encode("ascii"))
                )
            except (InvalidToken, ValueError, UnicodeEncodeError):
                return Response(
                    {"detail": "Seed artifact integrity validation failed."},
                    status=503,
                )
            if (
                not isinstance(seed, dict)
                or set(seed) != {"user-data", "meta-data", "network-config"}
            ):
                return Response({"detail": "Seed artifact payload is invalid."}, status=503)
            artifact.downloaded_at = now
            artifact.encrypted_payload = ""
            artifact.save(update_fields=["downloaded_at", "encrypted_payload"])

        response = HttpResponse(
            _json_seed_archive(seed),
            content_type="application/zip",
        )
        response["Content-Disposition"] = (
            'attachment; filename="eph-seed-{}.zip"'.format(artifact.agent_node_id)
        )
        response["Cache-Control"] = "no-store, max-age=0"
        response["Pragma"] = "no-cache"
        return response


class EPHSeedArtifactRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, tenant_id, project_id, node_id, artifact_id):
        tenant, project, is_admin = _seed_scope(request, tenant_id, project_id)
        if not is_admin:
            return Response({"detail": "Project administrator role is required."}, status=403)
        node = get_object_or_404(
            AgentNode.objects.filter(tenant=tenant, project=project),
            pk=node_id,
            node_type=AgentNode.NodeType.EPH_NODE,
        )
        with transaction.atomic():
            artifact = get_object_or_404(
                EPHSeedArtifact.objects.select_for_update(),
                pk=artifact_id,
                agent_node=node,
            )
            if artifact.downloaded_at is not None:
                return Response(
                    {"detail": "A downloaded seed artifact cannot be revoked."},
                    status=409,
                )
            if artifact.revoked_at is None:
                artifact.revoked_at = timezone.now()
                artifact.encrypted_payload = ""
                artifact.save(update_fields=["revoked_at", "encrypted_payload"])
                record_audit_event(
                    request,
                    tenant,
                    "agent.node.eph_seed_revoked",
                    "eph_seed_artifact",
                    artifact.id,
                    project=project,
                    metadata={"node_id": str(node.id), "artifact_id": str(artifact.id)},
                )
        return Response(
            {
                "artifact_id": str(artifact.id),
                "revoked": True,
                "revoked_at": artifact.revoked_at,
            }
        )
