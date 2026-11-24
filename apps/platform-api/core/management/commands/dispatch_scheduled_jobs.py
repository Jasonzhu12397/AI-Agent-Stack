"""Run the Control Agent turns whose schedule slot is due.

The worker calls the same service on every poll; this command exists so an
operator (or a cron-less deployment) can drain a backlog on demand.
"""

from django.core.management.base import BaseCommand

from core.scheduled_service import dispatch_due_jobs


class Command(BaseCommand):
    help = "Dispatch every scheduled Control Agent turn that is due now."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        ran = dispatch_due_jobs(limit=max(1, int(options["limit"])))
        self.stdout.write(f"dispatched={ran}")
