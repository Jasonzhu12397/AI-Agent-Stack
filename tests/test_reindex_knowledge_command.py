from io import StringIO
from types import SimpleNamespace

import pytest
from core.management.commands import reindex_knowledge
from core.models import (
    KnowledgeBase,
    KnowledgeDocument,
    ModelConfig,
    ModelProvider,
    Project,
    Tenant,
)
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError


class FakeStore:
    def __init__(self, base="agent_knowledge"):
        self.collection = base

    def collection_for_model(self, model_name, vector_size):
        return f"{self.collection}__{model_name.replace('/', '-')}-{vector_size}"


@pytest.fixture
def document(db):
    user = get_user_model().objects.create_user(username="reindex-admin")
    tenant = Tenant.objects.create(name="Reindex tenant", slug="reindex-tenant")
    project = Project.objects.create(tenant=tenant, name="Reindex project", slug="reindex-project")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Reindex provider",
        provider_type=ModelProvider.ProviderType.FAKE,
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="bge-m3",
        display_name="BGE M3",
        capabilities=["embeddings"],
    )
    knowledge_base = KnowledgeBase.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        embedding_model=model,
        name="Reindex KB",
        slug="reindex-kb",
    )
    return KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base,
        uploaded_by=user,
        title="Reindex document",
        content="content" * 200,
        metadata={"source": "unit-test"},
        checksum="c" * 64,
        status=KnowledgeDocument.Status.READY,
        chunk_count=3,
        vector_collection="",
        vector_dimensions=None,
    )


def test_scope_check_ignores_dimension_but_not_model():
    store = FakeStore()
    document = SimpleNamespace(
        vector_collection="agent_knowledge__bge-m3-1024",
        knowledge_base=SimpleNamespace(
            embedding_model=SimpleNamespace(model_name="bge-m3"),
        ),
    )
    assert reindex_knowledge.is_scope_current(document, store) is True

    document.vector_collection = "agent_knowledge__text-embedding-3-small-1024"
    assert reindex_knowledge.is_scope_current(document, store) is False

    document.vector_collection = "agent_knowledge"
    assert reindex_knowledge.is_scope_current(document, store) is False

    document.vector_collection = ""
    assert reindex_knowledge.is_scope_current(document, store) is False


@pytest.mark.django_db
def test_reindex_embeds_documents_into_the_model_collection(document):
    calls = []

    def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {
            "chunks": 4,
            "collection": "agent_knowledge__bge-m3-8",
            "dimensions": 8,
        }

    summary = reindex_knowledge.reindex_documents(
        [document],
        FakeStore(),
        fake_ingest,
    )

    assert summary["planned"] == [str(document.id)]
    assert len(summary["reindexed"]) == 1
    assert summary["skipped_current"] == 0
    assert summary["failed"] == []
    assert calls[0]["document_id"] == str(document.id)
    assert calls[0]["metadata"]["knowledge_base_id"] == str(document.knowledge_base_id)
    assert calls[0]["metadata"]["title"] == "Reindex document"
    assert calls[0]["metadata"]["source"] == "unit-test"

    document.refresh_from_db()
    assert document.chunk_count == 4
    assert document.vector_collection == "agent_knowledge__bge-m3-8"
    assert document.vector_dimensions == 8
    assert document.ingested_at is not None


@pytest.mark.django_db
def test_reindex_skips_current_documents_unless_forced(document):
    document.vector_collection = "agent_knowledge__bge-m3-8"
    document.save(update_fields=["vector_collection"])

    def fake_ingest(**kwargs):
        raise AssertionError("ingest must not run for a current document")

    summary = reindex_knowledge.reindex_documents([document], FakeStore(), fake_ingest)
    assert summary["planned"] == []
    assert summary["skipped_current"] == 1

    summary = reindex_knowledge.reindex_documents(
        [document],
        FakeStore(),
        lambda **kwargs: {"chunks": 4, "collection": "agent_knowledge__bge-m3-8", "dimensions": 8},
        force=True,
    )
    assert summary["planned"] == [str(document.id)]
    assert len(summary["reindexed"]) == 1


@pytest.mark.django_db
def test_reindex_isolates_failures_and_skips_unready_documents(document):
    def failing_ingest(**kwargs):
        raise reindex_knowledge.RagRuntimeError("embedding provider rejected the request")

    summary = reindex_knowledge.reindex_documents([document], FakeStore(), failing_ingest)
    assert summary["failed"] == [
        {
            "document_id": str(document.id),
            "error": "embedding provider rejected the request",
        }
    ]
    assert summary["reindexed"] == []
    document.refresh_from_db()
    assert document.vector_collection == ""

    document.status = KnowledgeDocument.Status.INGESTING
    document.save(update_fields=["status"])
    summary = reindex_knowledge.reindex_documents([document], FakeStore(), failing_ingest)
    assert summary["planned"] == []
    assert summary["skipped_not_ready"] == 1
    assert summary["failed"] == []


@pytest.mark.django_db
def test_dry_run_reports_plan_without_writing(document):
    def fake_ingest(**kwargs):
        raise AssertionError("dry run must not call ingest")

    summary = reindex_knowledge.reindex_documents(
        [document], FakeStore(), fake_ingest, dry_run=True
    )
    assert summary["planned"] == [str(document.id)]
    assert summary["reindexed"] == []
    document.refresh_from_db()
    assert document.vector_collection == ""


@pytest.mark.django_db
def test_command_validates_inputs_and_unknown_documents(document, monkeypatch):
    monkeypatch.setattr(reindex_knowledge, "QdrantVectorStore", FakeStore)

    with pytest.raises(CommandError, match="--limit"):
        call_command("reindex_knowledge", limit=10**6)
    with pytest.raises(CommandError, match="Invalid document id"):
        call_command("reindex_knowledge", document=["not-a-uuid"])

    stdout = StringIO()
    call_command("reindex_knowledge", document=[str(document.id)], dry_run=True, stdout=stdout)
    assert "candidates=1 planned=1 reindexed=0" in stdout.getvalue()
