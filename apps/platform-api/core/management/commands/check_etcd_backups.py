"""Turn "is this cluster still backed up?" into a signal a machine can read.

The scheduler takes backups; it has no way to tell that a cluster stopped
reporting them. This command walks every installed cluster, asks the same
question the operator panel asks, writes an audit event for each problem and
exits non-zero, so a cron entry, a monitoring probe or CI can alarm without a
human watching a table.
"""

from __future__ import annotations

import json
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.audit import record_audit_event
from core.etcd_backup_state import etcd_backup_state
from core.models import AuditEvent, ClusterDeployment

PROBLEM_CODES = frozenset(
    {"ETCD_BACKUP_MISSING", "ETCD_BACKUP_STALE", "ETCD_BACKUP_NOT_RUN_YET"}
)


class Command(BaseCommand):
    help = "Report, and audit, every installed cluster whose backups are missing or old."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-audit", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        clusters = []
        problems = []
        deployments = (
            ClusterDeployment.objects.filter(status=ClusterDeployment.Status.SUCCEEDED)
            .select_related("tenant", "project", "target_node")
            .order_by("id")
        )
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
                "last_run_status": (state.get("last_run") or {}).get("status"),
            }
            clusters.append(entry)
            if code == "" or code == "ETCD_BACKUP_DISABLED":
                continue
            problems.append(entry)
            if options["no_audit"] or options["dry_run"]:
                continue
            record_audit_event(
                None,
                deployment.tenant,
                "cluster.etcd_backup.problem",
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


def _stamp(now: datetime) -> str:
    return now.isoformat()
