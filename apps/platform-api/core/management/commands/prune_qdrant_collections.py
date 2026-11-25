"""List or delete Qdrant collections no longer referenced by a document.

The reindex command creates a new collection per embedding model and vector
dimension. Old collections are intentionally retained until an operator asks
for a cleanup; this command is that explicit, auditable cleanup path. It never
deletes a collection that a KnowledgeDocument still references, and it only
touches names under the configured collection prefix.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import KnowledgeDocument
from core.vector_store import QdrantVectorStore, VectorStoreError


class Command(BaseCommand):
    help = "Report or delete Qdrant collections that no KnowledgeDocument references."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the reported collections. Without this flag the command is read-only.",
        )
        parser.add_argument(
            "--prefix",
            default="",
            help=(
                "Only consider collection names with this prefix. Defaults to "
                "'<configured base collection>__'."
            ),
        )

    def handle(self, *args, **options):
        try:
            store = QdrantVectorStore()
            base_prefix = store.collection + "__"
        except VectorStoreError as exc:
            raise CommandError(str(exc)) from exc
        prefix = str(options.get("prefix") or base_prefix)
        if not prefix.startswith(base_prefix):
            raise CommandError(
                f"Refusing to prune outside the configured collection namespace ({base_prefix})."
            )
        try:
            store._validate_collection_name(prefix.rstrip("_") or store.collection)
        except VectorStoreError as exc:
            raise CommandError(str(exc)) from exc

        active = set(
            KnowledgeDocument.objects.exclude(vector_collection="")
            .values_list("vector_collection", flat=True)
            .distinct()
        )
        try:
            collections = store.list_collections()
        except VectorStoreError as exc:
            raise CommandError(str(exc)) from exc

        candidates = [
            name
            for name in collections
            if name.startswith(prefix) and name not in active
        ]
        deleted = []
        failures = []
        if options["apply"]:
            for name in candidates:
                try:
                    store.delete_collection(name, allow_missing=True)
                except VectorStoreError as exc:
                    failures.append({"collection": name, "error": str(exc)})
                else:
                    deleted.append(name)

        self.stdout.write(
            " ".join(
                [
                    f"total={len(collections)}",
                    f"active={len(active)}",
                    f"candidates={len(candidates)}",
                    f"deleted={len(deleted)}",
                    f"mode={'apply' if options['apply'] else 'dry-run'}",
                ]
            )
        )
        for name in candidates:
            self.stdout.write(f"candidate collection={name}")
        for failure in failures:
            self.stderr.write(
                f"failed collection={failure['collection']} error={failure['error']}"
            )
        if failures:
            raise CommandError(f"{len(failures)} collection deletion(s) failed.")
