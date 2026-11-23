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

from core.audit import record_audit_event
from core.etcd_backup_queue import hour_bucket, queue_backup, unqueueable_reason
from core.installed_clusters import installed_deployments
from core.models import AuditEvent, Tenant

SCHEDULER_EVENT = "cluster.etcd_backup.scheduler.run"


def _record_heartbeat(bucket: str, queued: list, skipped: list) -> None:
    """Say that the scheduler is alive, once per hour bucket.

    A scheduler that dies is invisible: the panel keeps showing the last backup
    it took, and nobody learns that nothing is queued any more until the backups
    are old enough to look stale. One marker per hour is enough to tell "quiet
    because nothing changed" from "quiet because the process is gone".
    """

    for tenant in Tenant.objects.all():
        event = (
            AuditEvent.objects.filter(
                tenant=tenant,
                event_type=SCHEDULER_EVENT,
                resource_type="etcd_backup_scheduler",
            )
            .only("metadata")
            .first()
        )
        metadata = event.metadata if event and isinstance(event.metadata, dict) else {}
        if metadata.get("bucket") == bucket:
            continue
        record_audit_event(
            None,
            tenant,
            SCHEDULER_EVENT,
            "etcd_backup_scheduler",
            "",
            outcome=AuditEvent.Outcome.SUCCESS,
            metadata={
                "bucket": bucket,
                "queued": len(queued),
                "skipped": len(skipped),
            },
        )


class Command(BaseCommand):
    help = "Queue cluster.kubernetes.backup.run for every installed Target cluster."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        # No default: an unset flag must not overwrite the deployment's own
        # policy, and 0 is only meaningful where the contract defines it (an
        # explicit min-free-bytes=0 means "measure, do not refuse").
        parser.add_argument(
            "--directory", default=None, help="override the backup directory"
        )
        parser.add_argument("--keep-count", type=int, default=None)
        parser.add_argument("--keep-max-age-hours", type=int, default=None)
        parser.add_argument("--pre-upgrade-keep-max-age-hours", type=int, default=None)
        parser.add_argument(
            "--min-free-bytes",
            type=int,
            default=None,
            help="override the free-space floor; 0 measures without refusing",
        )

    def handle(self, *args, **options):
        queued = []
        skipped = []
        deployments = installed_deployments()
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
        if not options["dry_run"]:
            _record_heartbeat(bucket, queued, skipped)
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
