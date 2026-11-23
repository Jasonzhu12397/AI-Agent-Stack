"""Release snapshot protection from rollbacks that can never run.

An approved rollback that no agent ever claimed — the Target Agent is offline
for good — would otherwise hold its snapshot forever, and retention could
never prune it. This command runs the same horizon sweep the policy layer owns:
each approved, unclaimed rollback past its deployment's protection horizon
moves to the terminal ``expired`` state, releases the lease fields and writes
one audit event. Running or claimed rollbacks are left alone.

The sweep is idempotent, so an external timer (cron, systemd timer, compose
healthcheck loop) may fire it repeatedly without double transitions or
duplicate audit events.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.etcd_backup_policy import release_expired_rollback_protection


class Command(BaseCommand):
    help = (
        "Release snapshot protection from approved rollbacks never claimed "
        "past their protection horizon."
    )

    def handle(self, *args, **options):
        released = release_expired_rollback_protection()
        self.stdout.write(
            json.dumps(
                {
                    "released": [str(command_id) for command_id in released],
                    "released_count": len(released),
                    "at": timezone.now().isoformat(),
                },
                ensure_ascii=False,
            )
        )
        return None
