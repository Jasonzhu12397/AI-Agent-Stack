"""Queue the recurring etcd backup for every installed cluster.

An external scheduler (compose healthcheck loop, systemd timer or cron) runs
this command; the command itself is idempotent per hour bucket, so a scheduler
that fires twice in the same hour enqueues one backup, not two. It never
approves anything by itself: a backup needs no approval, but it is dispatched
as an ordinary command, so leases, at-most-once delivery and audit all apply.

Nothing is queued for a cluster whose deployment has no Target Agent, or whose
spec declares ``kubernetes.etcd_backup` false.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.etcd_backup_queue import hour_bucket, queue_backup, unqueueable_reason
from core.models import ClusterDeployment


class Command(BaseCommand):
    help = "Queue cluster.kubernetes.backup.run for every installed Target cluster."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--directory", default="")
        parser.add_argument("--keep-count", type=int, default=0)
        parser.add_argument("--keep-max-age-hours", type=int, default=0)
        parser.add_argument("--pre-upgrade-keep-max-age-hours", type=int, default=0)
        parser.add_argument("--min-free-bytes", type=int, default=0)

    def handle(self, *args, **options):
        queued = []
        skipped = []
        deployments = (
            ClusterDeployment.objects.filter(
                status=ClusterDeployment.Status.SUCCEEDED,
            )
            .select_related("target_node", "tenant", "project")
            .order_by("id")
        )
        bucket = hour_bucket()
        overrides = {
            "directory": options["directory"],
            "keep_count": options["keep_count"],
            "keep_max_age_hours": options["keep_max_age_hours"],
            "pre_upgrade_keep_max_age_hours": options["pre_upgrade_keep_max_age_hours"],
            "min_free_bytes": options["min_free_bytes"],
        }
        for deployment in deployments:
            if options["dry_run"]:
                # A dry run must not write, but it must still say why a cluster
                # would be skipped, so it reports the same reasons.
                reason = unqueueable_reason(deployment)
                if reason:
                    skipped.append(
                        {"deployment_id": str(deployment.id), "reason": reason}
                    )
                else:
                    queued.append(
                        {"deployment_id": str(deployment.id), "command_id": None}
                    )
                continue
            command, created, reason = queue_backup(
                deployment, overrides=overrides
            )
            if reason:
                skipped.append({"deployment_id": str(deployment.id), "reason": reason})
                continue
            queued.append(
                {
                    "deployment_id": str(deployment.id),
                    "command_id": str(command.id),
                    "created": created,
                }
            )
        self.stdout.write(
            json.dumps(
                {
                    "bucket": bucket,
                    "queued": queued,
                    "skipped": skipped,
                    "dry_run": bool(options["dry_run"]),
                    "at": timezone.now().isoformat(),
                },
                ensure_ascii=False,
            )
        )
        return None
