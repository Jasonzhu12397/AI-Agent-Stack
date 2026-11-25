"""Register the connectors the composer offers in its plugin menu."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant
from core.plugin_catalog import catalog_file, load_catalog_entries, sync_tenant_catalog


class Command(BaseCommand):
    help = "Upsert the platform connector catalog for tenants."

    def add_arguments(self, parser):
        parser.add_argument("--catalog", default="")
        parser.add_argument("--tenant", default="")

    def handle(self, *args, **options):
        path = catalog_file(Path(options["catalog"]) if options["catalog"] else None)
        if not path.exists():
            raise CommandError(f"catalog file not found: {path}")
        entries = load_catalog_entries(path)
        tenants = Tenant.objects.order_by("created_at")
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])
        created = enabled = 0
        for tenant in tenants:
            owner = tenant.memberships.order_by("created_at").first()
            if owner is None:
                continue
            tenant_created, tenant_enabled = sync_tenant_catalog(
                tenant, owner_id=owner.user_id, entries=entries
            )
            created += tenant_created
            enabled += tenant_enabled
        self.stdout.write(f"catalog entries={created} enabled={enabled}")
