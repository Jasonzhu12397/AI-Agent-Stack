import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import KnowledgeDocument
from core.rag_runtime import RagRuntimeError, ingest_text
from core.vector_store import QdrantVectorStore, VectorStoreError

MAX_LIMIT = 5000


def collection_prefix_for_model(store, model_name):
    """Return the collection-name prefix owned by an embedding model.

    ``collection_for_model`` appends ``-<dimension>`` to a ``<base>__<slug>``
    collection, so the prefix is derived by stripping that suffix. Deriving it
    here keeps the check independent of the vector dimension, which is only
    known after the first embedding call.
    """

    scoped = store.collection_for_model(model_name, 1)
    return scoped.rsplit("-", 1)[0] + "-"


def is_scope_current(document, store):
    """Return True when the document already lives in its model's collection."""

    if not document.vector_collection:
        return False
    prefix = collection_prefix_for_model(store, document.knowledge_base.embedding_model.model_name)
    return document.vector_collection.startswith(prefix)


def reindex_documents(documents, store, ingest, *, force=False, dry_run=False):
    """Re-embed documents into their current model collection.

    Returns a summary dict. One failing document never aborts the run: failures
    are collected so an operator can retry only those documents.
    """

    summary = {
        "planned": [],
        "reindexed": [],
        "skipped_current": 0,
        "skipped_not_ready": 0,
        "failed": [],
    }
    for document in documents:
        if document.status != KnowledgeDocument.Status.READY:
            summary["skipped_not_ready"] += 1
            continue
        try:
            needs_reindex = force or not is_scope_current(document, store)
        except VectorStoreError as exc:
            summary["failed"].append({"document_id": str(document.id), "error": str(exc)})
            continue
        if not needs_reindex:
            summary["skipped_current"] += 1
            continue
        summary["planned"].append(str(document.id))
        if dry_run:
            continue
        knowledge_base = document.knowledge_base
        try:
            result = ingest(
                tenant_id=str(knowledge_base.tenant_id),
                project_id=str(knowledge_base.project_id),
                document_id=str(document.id),
                text=document.content,
                embedding_model=knowledge_base.embedding_model,
                metadata={
                    **document.metadata,
                    "title": document.title,
                    "knowledge_base_id": str(knowledge_base.id),
                },
                chunk_size=knowledge_base.chunk_size,
                overlap=knowledge_base.chunk_overlap,
            )
        except (RagRuntimeError, VectorStoreError) as exc:
            summary["failed"].append({"document_id": str(document.id), "error": str(exc)})
            continue
        with transaction.atomic():
            document.chunk_count = result["chunks"]
            document.vector_collection = result.get("collection") or ""
            document.vector_dimensions = result.get("dimensions")
            document.ingested_at = timezone.now()
            document.save(
                update_fields=[
                    "chunk_count",
                    "vector_collection",
                    "vector_dimensions",
                    "ingested_at",
                    "updated_at",
                ]
            )
        summary["reindexed"].append(
            {
                "document_id": str(document.id),
                "collection": document.vector_collection,
                "dimensions": document.vector_dimensions,
                "chunks": document.chunk_count,
            }
        )
    return summary


class Command(BaseCommand):
    help = (
        "Re-embed knowledge documents into the Qdrant collection owned by their "
        "current embedding model. Legacy collections are left untouched."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document",
            action="append",
            default=[],
            metavar="UUID",
            help="Only reindex the given document; repeat the flag to pass several.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help=f"Reindex at most this many documents (0 means no limit, max {MAX_LIMIT}).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-embed even when the document already sits in its model collection.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be reindexed without calling the model or the vector store.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 0 or limit > MAX_LIMIT:
            raise CommandError(f"--limit must be between 0 and {MAX_LIMIT}.")
        document_ids = []
        for raw_id in options["document"]:
            try:
                document_ids.append(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError) as exc:
                raise CommandError(f"Invalid document id: {raw_id}") from exc
        documents = KnowledgeDocument.objects.select_related(
            "knowledge_base",
            "knowledge_base__embedding_model",
            "knowledge_base__embedding_model__provider",
        ).filter(knowledge_base__is_archived=False)
        if document_ids:
            requested = set(document_ids)
            available = set(documents.filter(pk__in=requested).values_list("id", flat=True))
            missing = requested - available
            if missing:
                self.stderr.write(
                    "Unknown or archived document ids: "
                    + ", ".join(str(item) for item in sorted(missing))
                )
            documents = documents.filter(pk__in=available)
        documents = documents.order_by("created_at")
        if limit:
            documents = documents[:limit]
        candidates = list(documents)
        try:
            store = QdrantVectorStore()
        except VectorStoreError as exc:
            raise CommandError(str(exc)) from exc
        summary = reindex_documents(
            candidates,
            store,
            ingest_text,
            force=options["force"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            f"candidates={len(candidates)} planned={len(summary['planned'])} "
            f"reindexed={len(summary['reindexed'])} "
            f"skipped_current={summary['skipped_current']} "
            f"skipped_not_ready={summary['skipped_not_ready']} "
            f"failed={len(summary['failed'])}"
        )
        for item in summary["planned"]:
            self.stdout.write(f"planned document={item}")
        for item in summary["reindexed"]:
            self.stdout.write(
                f"reindexed document={item['document_id']} "
                f"collection={item['collection']} "
                f"dimensions={item['dimensions']} chunks={item['chunks']}"
            )
        for item in summary["failed"]:
            self.stderr.write(f"failed document={item['document_id']} error={item['error']}")
        if summary["failed"]:
            raise CommandError(
                f"{len(summary['failed'])} document(s) failed to reindex; rerun with "
                "--document to retry them."
            )
