import hashlib
import json
import re
import uuid

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import (
    KnowledgeBase,
    KnowledgeDocument,
    ModelConfig,
    Project,
    ProjectMembership,
    Task,
    Tenant,
    TenantMembership,
)


def _tenant_and_project(request, tenant_id, project_id):
    tenant = get_object_or_404(
        Tenant.objects.filter(memberships__user=request.user).distinct(),
        pk=tenant_id,
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


def _can_edit(request, tenant, project):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=request.user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_editor = ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()
    return tenant_admin or project_editor


def _contains_secret_key(value):
    forbidden = {
        "api_key",
        "apikey",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "authorization",
        "private_key",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or _contains_secret_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    return False


def _knowledge_base_data(item):
    return {
        "id": str(item.id),
        "tenant_id": str(item.tenant_id),
        "project_id": str(item.project_id),
        "name": item.name,
        "slug": item.slug,
        "description": item.description,
        "embedding_model_id": str(item.embedding_model_id),
        "chunk_size": item.chunk_size,
        "chunk_overlap": item.chunk_overlap,
        "is_archived": item.is_archived,
        "document_count": item.documents.count(),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _document_data(item):
    return {
        "id": str(item.id),
        "knowledge_base_id": str(item.knowledge_base_id),
        "title": item.title,
        "metadata": item.metadata,
        "checksum": item.checksum,
        "status": item.status,
        "chunk_count": item.chunk_count,
        "error": item.error,
        "ingested_at": item.ingested_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


class KnowledgeBaseListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project = _tenant_and_project(request, tenant_id, project_id)
        items = KnowledgeBase.objects.filter(
            tenant=tenant,
            project=project,
            is_archived=False,
        ).select_related("embedding_model")
        return Response(
            {
                "count": items.count(),
                "results": [_knowledge_base_data(item) for item in items[:100]],
            }
        )

    def post(self, request, tenant_id, project_id):
        tenant, project = _tenant_and_project(request, tenant_id, project_id)
        if not _can_edit(request, tenant, project):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        name = request.data.get("name")
        slug = request.data.get("slug")
        description = request.data.get("description", "")
        model_id = request.data.get("embedding_model_id")
        chunk_size = request.data.get("chunk_size", 1200)
        chunk_overlap = request.data.get("chunk_overlap", 160)
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            return Response(
                {"detail": "name is required and must be at most 120 characters."},
                status=400,
            )
        if not isinstance(slug, str) or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?",
            slug,
        ):
            return Response({"detail": "slug must be a lowercase URL-safe identifier."}, status=400)
        if not isinstance(description, str) or len(description) > 4000:
            return Response(
                {"detail": "description must be a string up to 4,000 characters."},
                status=400,
            )
        try:
            model_uuid = uuid.UUID(str(model_id))
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "A valid embedding_model_id is required."}, status=400)
        model = get_object_or_404(
            ModelConfig.objects.select_related("provider").filter(
                Q(provider__tenant=tenant) | Q(provider__tenant__isnull=True),
                enabled=True,
                provider__enabled=True,
            ),
            pk=model_uuid,
        )
        if "embeddings" not in (model.capabilities or []):
            return Response(
                {"detail": "Selected model does not declare embeddings capability."},
                status=400,
            )
        if (
            not isinstance(chunk_size, int)
            or isinstance(chunk_size, bool)
            or not 200 <= chunk_size <= 8000
            or not isinstance(chunk_overlap, int)
            or isinstance(chunk_overlap, bool)
            or not 0 <= chunk_overlap < chunk_size
        ):
            return Response({"detail": "Invalid chunk_size or chunk_overlap."}, status=400)
        if KnowledgeBase.objects.filter(project=project, slug=slug).exists():
            return Response(
                {"detail": "Knowledge base slug already exists in this project."},
                status=409,
            )
        item = KnowledgeBase.objects.create(
            tenant=tenant,
            project=project,
            created_by=request.user,
            embedding_model=model,
            name=name.strip(),
            slug=slug,
            description=description,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        return Response(_knowledge_base_data(item), status=status.HTTP_201_CREATED)


class KnowledgeBaseDocumentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_knowledge_base(self, request, knowledge_base_id):
        queryset = KnowledgeBase.objects.filter(
            tenant__memberships__user=request.user,
            is_archived=False,
        ).select_related("tenant", "project")
        return get_object_or_404(queryset.distinct(), pk=knowledge_base_id)

    def get(self, request, knowledge_base_id):
        item = self.get_knowledge_base(request, knowledge_base_id)
        if not ProjectMembership.objects.filter(
            project=item.project,
            user=request.user,
        ).exists() and not TenantMembership.objects.filter(
            tenant=item.tenant,
            user=request.user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists():
            return Response({"detail": "Not found."}, status=404)
        documents = item.documents.all()
        return Response(
            {
                "count": documents.count(),
                "results": [_document_data(document) for document in documents[:100]],
            }
        )

    def post(self, request, knowledge_base_id):
        item = self.get_knowledge_base(request, knowledge_base_id)
        if not _can_edit(request, item.tenant, item.project):
            return Response(
                {"detail": "Project editor permission required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        title = request.data.get("title")
        content = request.data.get("content")
        metadata = request.data.get("metadata", {})
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            return Response(
                {"detail": "title is required and must be at most 240 characters."},
                status=400,
            )
        if not isinstance(content, str) or not content.strip() or len(content) > 1_000_000:
            return Response(
                {"detail": "content must be non-empty and no larger than 1 MB."},
                status=400,
            )
        if not isinstance(metadata, dict) or len(json.dumps(metadata)) > 16384:
            return Response({"detail": "metadata must be a JSON object up to 16 KiB."}, status=400)
        if _contains_secret_key(metadata):
            return Response(
                {"detail": "Document metadata cannot contain credential fields."},
                status=400,
            )
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with transaction.atomic():
            document = KnowledgeDocument.objects.create(
                knowledge_base=item,
                uploaded_by=request.user,
                title=title.strip(),
                content=content,
                metadata=metadata,
                checksum=checksum,
            )
            Task.objects.create(
                tenant=item.tenant,
                project=item.project,
                created_by=request.user,
                task_type="knowledge.ingest",
                payload={"knowledge_document_id": str(document.id)},
                idempotency_key=f"knowledge-ingest:{document.id}",
                available_at=timezone.now(),
            )
        return Response(_document_data(document), status=status.HTTP_202_ACCEPTED)
