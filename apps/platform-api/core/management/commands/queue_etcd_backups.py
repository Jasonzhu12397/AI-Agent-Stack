"""Queue the recurring etcd backup for every installed cluster.

An external scheduler (compose healthcheck loop, systemd timer or cron) runs
this command; the command itself is idempotent per hour bucket, so a scheduler
that fires twice in the same hour enqueues one backup, not two. It never
approves anything by itself: a backup needs no approval, but it is dispatched
as an ordinary command, so leases, at-most-once delivery and audit all apply.

Nothing is queued for a cluster whose deployment has no Target Agent, or whose
spec declares ``kubernetes.etcd_backup` false`.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.audit import record_audit_event
from core.etcd_backup_queue import hour_bucket, queue_backup, unqueueable_reason
from core.installed_clusters import installed_deployments
from core.models import AuditEvent, ClusterDeployment

SCHEDULER_EVENT = "cluster.etcd_backup.scheduler.run"
RESOURCE_TYPE = "etcd_backup_scheduler"

#: Outcome values stored on a per-cluster evaluation record.
RESULT_QUEUED = "queued"
RESULT_SKIPPED = "skipped"


def _record_heartbeat(
    bucket: str, deployment: ClusterDeployment, outcome: dict[str, Any]
) -> None:
    """Say that this cluster was evaluated in this hour bucket, and how.

    The record belongs to the deployment: it answers "did the scheduler look at
    *this* cluster this hour", not "did some command run on this tenant". A
    cluster that was skipped on purpose is recorded as skipped, with its reason,
    so the panel does not read the skip as a silent scheduler. One record per
    deployment per bucket: a second pass in the same hour adds nothing.
    """

    event = (
        AuditEvent.objects.filter(
            tenant=deployment.tenant,
            event_type=SCHEDULER_EVENT,
            resource_type=RESOURCE_TYPE,
            resource_id=str(deployment.id),
        )
        .only("metadata")
        .order_by("-created_at")
        .first()
    )
    metadata = event.metadata if event and isinstance(event.metadata, dict) else {}
    if metadata.get("bucket") == bucket:
        return

    payload: dict[str, Any] = {
        "bucket": bucket,
        "result": outcome["result"],
        "cluster_name": deployment.cluster_name,
    }
    if outcome["result"] == RESULT_SKIPPED:
        payload["reason"] = outcome["reason"]
    elif outcome.get("command_id"):
        payload["command_id"] = outcome["command_id"]

    record_audit_event(
        None,
        deployment.tenant,
        SCHEDULER_EVENT,
        RESOURCE_TYPE,
        str(deployment.id),
        project=deployment.project,
        outcome=AuditEvent.Outcome.SUCCESS,
        metadata=payload,
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
        outcomes: list[dict[str, Any]] = []
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
                outcomes.append(
                    {
                        "deployment": deployment,
                        "result": RESULT_SKIPPED,
                        "reason": reason,
                    }
                )
                continue
            queued.append(
                {
                    "deployment_id": str(deployment.id),
                    "command_id": str(command.id),
                    "created": created,
                }
            )
            outcomes.append(
                {
                    "deployment": deployment,
                    "result": RESULT_QUEUED,
                    "command_id": str(command.id),
                }
            )
        if not options["dry_run"]:
            for outcome in outcomes:
                _record_heartbeat(bucket, outcome["deployment"], outcome)
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
