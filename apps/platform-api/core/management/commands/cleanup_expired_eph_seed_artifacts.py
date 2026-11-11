from itertools import islice

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import EPHSeedArtifact


def _batches(values, batch_size):
    iterator = iter(values)
    for first in iterator:
        yield [first, *islice(iterator, batch_size - 1)]


class Command(BaseCommand):
    help = "Erase encrypted payloads from expired, undownloaded EPH seed artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1 or batch_size > 10000:
            raise CommandError("--batch-size must be between 1 and 10000.")

        expired = EPHSeedArtifact.objects.filter(
            expires_at__lte=timezone.now(),
            downloaded_at__isnull=True,
            revoked_at__isnull=True,
            encrypted_payload__gt="",
        ).order_by("expires_at", "id")
        total = expired.count()
        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY_RUN expired_undownloaded_artifacts={total} payloads_to_erase"
                )
            )
            return

        artifact_ids = expired.values_list("id", flat=True).iterator(
            chunk_size=batch_size
        )
        erased = 0
        for batch_ids in _batches(artifact_ids, batch_size):
            with transaction.atomic():
                erased += EPHSeedArtifact.objects.filter(
                    id__in=batch_ids,
                    expires_at__lte=timezone.now(),
                    downloaded_at__isnull=True,
                    revoked_at__isnull=True,
                    encrypted_payload__gt="",
                ).update(encrypted_payload="")

        self.stdout.write(
            self.style.SUCCESS(
                f"Expired EPH seed payloads erased={erased}; artifact metadata retained."
            )
        )
