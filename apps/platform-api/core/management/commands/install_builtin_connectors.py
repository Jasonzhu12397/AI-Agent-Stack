"""Install every enabled built-in connector into the tenant's default project.

Built-in connectors (map, weather) need no endpoint and no credential, so a
console that declares them enabled should also have them installed; otherwise
the plugin menu shows an "install" button for something the deployment already
supports. Project-installed connectors are idempotent: re-running keeps the
existing row and only refreshes the granted capability list.
"""

from django.core.management.base import BaseCommand

from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    Tenant,
)


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
            definitions = AgentPluginDefinition.objects.filter(
                tenant=tenant,
                enabled=True,
                protocol=AgentPluginDefinition.Protocol.OPEN_DATA,
            )
            for project in Project.objects.filter(tenant=tenant):
                for definition in definitions:
                    capabilities = [
                        item["name"]
                        for item in definition.capabilities or []
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    ]
                    row, created = AgentPluginInstallation.objects.get_or_create(
                        project=project,
                        plugin=definition,
                        defaults={
                            "tenant": tenant,
                            "plugin_version": definition.version,
                            "status": AgentPluginInstallation.Status.INSTALLED,
                            "granted_capabilities": capabilities,
                            "installed_by_id": owner.user_id,
                            "credential_ref": "",
                        },
                    )
                    if not created and row.status != AgentPluginInstallation.Status.INSTALLED:
                        row.status = AgentPluginInstallation.Status.INSTALLED
                        row.save(update_fields=["status", "updated_at"])
                    installed += 1
        self.stdout.write(f"builtin connectors installed={installed}")
