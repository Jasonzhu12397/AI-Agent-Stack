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

import hashlib
import json
from datetime import UTC, datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.etcd_backup_policy import protected_names, spec_policy
from core.models import AgentCommand, AgentNode, ClusterDeployment
from packages.agent_contracts.etcd_backup import ACTION, validate_request


def hour_bucket(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H")


def payload_hash(command_type: str, payload: dict) -> str:
    encoded = json.dumps(
        {"command_type": command_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        for deployment in deployments:
            spec = deployment.spec if isinstance(deployment.spec, dict) else {}
            kubernetes = spec.get("kubernetes") if isinstance(spec, dict) else None
            if isinstance(kubernetes, dict) and kubernetes.get("etcd_backup") is False:
                skipped.append({"deployment_id": str(deployment.id), "reason": "disabled"})
                continue
            node = deployment.target_node
            if node is None or node.node_type != AgentNode.NodeType.TARGET_CLUSTER:
                skipped.append(
                    {"deployment_id": str(deployment.id), "reason": "no_target_agent"}
                )
                continue
            payload = {
                "cluster_name": deployment.cluster_name,
                "execution_mode": "execute",
                "confirm_cluster_name": deployment.cluster_name,
            }
            protect = protected_names(deployment)
            if protect:
                payload["protect"] = protect
            policy = spec_policy(kubernetes)
            for field, value in policy.items():
                payload[field] = value
            for field, option in (
                ("directory", "directory"),
                ("keep_count", "keep_count"),
                ("keep_max_age_hours", "keep_max_age_hours"),
                ("pre_upgrade_keep_max_age_hours", "pre_upgrade_keep_max_age_hours"),
                ("min_free_bytes", "min_free_bytes"),
            ):
                if options[option]:
                    payload[field] = options[option]
            try:
                validate_request(payload)
            except ValueError as exc:
                skipped.append(
                    {
                        "deployment_id": str(deployment.id),
                        "reason": getattr(exc, "code", "invalid_request"),
                    }
                )
                continue
            key = f"etcd-backup:{deployment.id}:{bucket}"
            if options["dry_run"]:
                queued.append({"deployment_id": str(deployment.id), "command_id": None})
                continue
            with transaction.atomic():
                command, created = AgentCommand.objects.get_or_create(
                    tenant=deployment.tenant,
                    idempotency_key=key,
                    defaults={
                        "project": deployment.project,
                        "node": node,
                        "requested_by": deployment.requested_by,
                        "command_type": ACTION,
                        "payload": payload,
                        "status": AgentCommand.Status.QUEUED,
                        "plan_hash": payload_hash(ACTION, payload),
                        "deployment": deployment,
                    },
                )
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
