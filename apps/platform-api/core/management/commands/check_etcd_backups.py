"""Turn "is this cluster still backed up?" into a signal a machine can read.

The scheduler takes backups; it has no way to tell that a cluster stopped
reporting them. This command walks every installed cluster, asks the same
question the operator panel asks, writes an audit event for each problem and
exits non-zero, so a cron entry, a monitoring probe or CI can alarm without a
human watching a table.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.audit import record_audit_event
from core.etcd_backup_state import etcd_backup_state
from core.models import AuditEvent, ClusterDeployment

PROBLEM_CODES = frozenset(
    {"ETCD_BACKUP_MISSING", "ETCD_BACKUP_STALE", "ETCD_BACKUP_NOT_RUN_YET"}
)
PROBLEM_EVENT = "cluster.etcd_backup.problem"
RECOVERED_EVENT = "cluster.etcd_backup.recovered"
#: A problem that persists is written once, not once per probe: an audit chain
#: that repeats the same sentence every hour stops being read.
DEFAULT_REPEAT_AFTER_HOURS = 6


class Command(BaseCommand):
    help = "Report, and audit, every installed cluster whose backups are missing or old."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-audit", action="store_true")
        parser.add_argument(
            "--repeat-after-hours",
            type=float,
            default=DEFAULT_REPEAT_AFTER_HOURS,
            help=(
                "re-audit a problem that has not changed after this many hours; "
                "0 audits every run"
            ),
        )

    def handle(self, *args, **options):
        now = timezone.now()
        clusters = []
        problems = []
        deployments = (
            ClusterDeployment.objects.filter(status=ClusterDeployment.Status.SUCCEEDED)
            .select_related("tenant", "project", "target_node")
            .order_by("id")
        )
        repeat_after = timedelta(hours=float(options["repeat_after_hours"]))
        for deployment in deployments:
            state = etcd_backup_state(deployment, now=now)
            policy = state["policy"]
            if not policy.get("valid", False):
                code = policy["error"]["code"]
            elif not policy.get("enabled", True):
                code = "ETCD_BACKUP_DISABLED"
            else:
                codes = [
                    finding["code"]
                    for finding in state["findings"]
                    if not finding["ok"] and finding["code"]
                ]
                code = codes[0] if codes else ""
            entry = {
                "deployment_id": str(deployment.id),
                "cluster_name": deployment.cluster_name,
                "code": code,
                "ok": code == "",
                "newest": (state.get("newest") or {}).get("name"),
                "newest_age_hours": state.get("newest_age_hours"),
                "stale": state.get("stale"),
                "restore_point": (state.get("restore_point") or {}).get("name"),
                "last_run_status": (state.get("last_run") or {}).get("status"),
            }
            clusters.append(entry)
            if code == "" or code == "ETCD_BACKUP_DISABLED":
                # A cluster that used to be a problem and no longer is gets one
                # event closing it, so the chain says when it recovered instead
                # of leaving the last failure standing forever.
                kind, previous, _ = _last_report(deployment)
                if (
                    code == ""
                    and kind == PROBLEM_EVENT
                    and not options["no_audit"]
                    and not options["dry_run"]
                ):
                    record_audit_event(
                        None,
                        deployment.tenant,
                        RECOVERED_EVENT,
                        "cluster_deployment",
                        deployment.id,
                        project=deployment.project,
                        metadata={
                            "cluster_name": deployment.cluster_name,
                            "previous_code": previous,
                            "newest": (state.get("newest") or {}).get("name"),
                            "checked_at": _stamp(now),
                        },
                    )
                    entry["recovered"] = True
                continue
            problems.append(entry)
            if options["no_audit"] or options["dry_run"]:
                continue
            kind, previous, previous_at = _last_report(deployment)
            if (
                kind == PROBLEM_EVENT
                and previous == code
                and previous_at is not None
                and repeat_after > timedelta(0)
                and now - previous_at < repeat_after
            ):
                entry["audited"] = False
                continue
            entry["audited"] = True
            record_audit_event(
                None,
                deployment.tenant,
                PROBLEM_EVENT,
                "cluster_deployment",
                deployment.id,
                project=deployment.project,
                outcome=AuditEvent.Outcome.FAILURE,
                metadata={
                    "code": code,
                    "cluster_name": deployment.cluster_name,
                    "newest": entry["newest"],
                    "newest_age_hours": entry["newest_age_hours"],
                    "checked_at": _stamp(now),
                },
            )
        report = {
            "checked_at": _stamp(now),
            "dry_run": bool(options["dry_run"]),
            "audited": not options["no_audit"],
            "clusters": clusters,
            "problems": problems,
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False))
        if problems:
            listed = ", ".join(
                f"{item['cluster_name']}/{item['code']}" for item in problems
            )
            raise CommandError(
                f"{len(problems)} of {len(clusters)} installed cluster(s) have no "
                f"usable recent backup: {listed}",
                returncode=1,
            )


def _last_report(
    deployment: ClusterDeployment,
) -> tuple[str, str, datetime | None]:
    """The last thing this watchdog said about the cluster: kind, code, when.

    Both kinds are read in one query, because "is the newest word a problem?"
    is the only question worth asking: a recovery is news exactly once, and a
    problem is news when its code changes.
    """

    event = (
        AuditEvent.objects.filter(
            tenant=deployment.tenant,
            event_type__in=(PROBLEM_EVENT, RECOVERED_EVENT),
            resource_type="cluster_deployment",
            resource_id=str(deployment.id),
        )
        .only("event_type", "metadata", "created_at")
        .first()
    )
    if event is None:
        return "", "", None
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    code = metadata.get("code") if event.event_type == PROBLEM_EVENT else ""
    return event.event_type, str(code or ""), event.created_at


def _stamp(now: datetime) -> str:
    return now.isoformat()
