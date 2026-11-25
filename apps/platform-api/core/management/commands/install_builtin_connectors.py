"""Install enabled built-in connectors into every tenant's projects."""

from django.core.management.base import BaseCommand

from core.models import Tenant
from core.plugin_catalog import install_builtin_connectors


class Command(BaseCommand):
    help = "Install enabled built-in connectors into every tenant's projects."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="", help="Limit to one tenant slug")

    def handle(self, *args, **options):
        tenants = Tenant.objects.order_by("created_at")
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])
        installed = 0
        for tenant in tenants:
            owner = tenant.memberships.order_by("created_at").first()
            if owner is None:
                continue
            installed += install_builtin_connectors(tenant, owner_id=owner.user_id)
        self.stdout.write(f"builtin connectors installed={installed}")
