from io import StringIO

import pytest
from core.management.commands import prune_qdrant_collections
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
    def __init__(self):
        self.collection = "agent_knowledge"
        self.deleted = []
        self.names = [
            "agent_knowledge",
            "agent_knowledge__bge-m3-1024",
            "agent_knowledge__bge-m3-8",
            "agent_knowledge__legacy-768",
            "another_app",
        ]

    @staticmethod
    def _validate_collection_name(name):
        if not name or "/" in name:
            raise ValueError(name)

    def list_collections(self):
        return self.names

    def delete_collection(self, name, allow_missing=False):
        self.deleted.append(name)
        return {"deleted": True, "collection": name}


@pytest.fixture
def active_collection(db):
    user = get_user_model().objects.create_user(username="prune-admin")
    tenant = Tenant.objects.create(name="Prune tenant", slug="prune-tenant")
    project = Project.objects.create(tenant=tenant, name="Prune project", slug="prune-project")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Prune provider",
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
        name="Prune KB",
        slug="prune-kb",
    )
    return KnowledgeDocument.objects.create(
        knowledge_base=knowledge_base,
        uploaded_by=user,
        title="Current document",
        content="content",
        checksum="p" * 64,
        status=KnowledgeDocument.Status.READY,
        chunk_count=1,
        vector_collection="agent_knowledge__bge-m3-1024",
    )


@pytest.mark.django_db
def test_prune_is_read_only_without_apply(monkeypatch, active_collection):
    store = FakeStore()
    monkeypatch.setattr(prune_qdrant_collections, "QdrantVectorStore", lambda: store)
    output = StringIO()
    call_command("prune_qdrant_collections", stdout=output)
    assert store.deleted == []
    assert "candidates=2" in output.getvalue()
    assert "agent_knowledge__bge-m3-8" in output.getvalue()
    assert "agent_knowledge__legacy-768" in output.getvalue()


@pytest.mark.django_db
def test_prune_apply_deletes_only_unreferenced_namespaced_collections(
    monkeypatch, active_collection
):
    store = FakeStore()
    monkeypatch.setattr(prune_qdrant_collections, "QdrantVectorStore", lambda: store)
    call_command("prune_qdrant_collections", "--apply", stdout=StringIO())
    assert store.deleted == [
        "agent_knowledge__bge-m3-8",
        "agent_knowledge__legacy-768",
    ]
    assert "agent_knowledge__bge-m3-1024" not in store.deleted
    assert "another_app" not in store.deleted
    assert "agent_knowledge" not in store.deleted


@pytest.mark.django_db
def test_prune_refuses_a_prefix_outside_the_configured_namespace(
    monkeypatch, active_collection
):
    monkeypatch.setattr(prune_qdrant_collections, "QdrantVectorStore", FakeStore)
    with pytest.raises(CommandError, match="configured collection namespace"):
        call_command("prune_qdrant_collections", "--prefix", "another_app__")
