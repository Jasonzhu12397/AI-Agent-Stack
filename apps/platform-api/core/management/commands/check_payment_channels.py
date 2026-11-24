"""Answer "why can't anyone pay?" from a shell.

Checkout is unavailable whenever no payment channel resolves its credentials,
and the HTTP response only says ``PAYMENT_PROVIDER_NOT_CONFIGURED``. This
command prints the per-channel slot gaps the capabilities endpoint reports, so
a deploy check or a cron probe fails loudly with a named cause instead of
leaving an operator to guess which merchant credential is missing.

Slot names only: no reference URI, no secret value is ever printed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.payments.base import payment_env_prefixes
from core.payments.registry import build_providers, diagnose_channels


class Command(BaseCommand):
    help = "Report which payment channels are configured and which credential slots are missing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-none",
            action="store_true",
            help="Exit zero even when no channel is configured (report-only use).",
        )

    def handle(self, *args, **options):
        entries = diagnose_channels()
        for entry in entries:
            if entry["configured"]:
                self.stdout.write(f"{entry['channel']}: configured")
            else:
                self.stdout.write(
                    f"{entry['channel']}: not configured, missing {', '.join(entry['missing'])}"
                )

        providers = build_providers()
        self.stdout.write(f"usable channels: {', '.join(sorted(providers)) or 'none'}")
        self.stdout.write("environment prefixes: " + ", ".join(payment_env_prefixes()))

        if providers or options["allow_none"]:
            return

        gaps = "; ".join(
            f"{entry['channel']} missing {', '.join(entry['missing'])}" for entry in entries
        )
        raise CommandError(
            "PAYMENT_PROVIDER_NOT_CONFIGURED: no payment channel resolves its "
            f"credentials ({gaps}); checkout is unavailable until one does."
        )
