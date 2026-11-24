"""Register the machine that hosts the console as a local runner.

The console can target a turn at *the local computer*; that requires an enrolled
``local_runner`` node plus the agent that leases its commands. This command
creates or refreshes that node and writes the bearer token the agent uses, so
development and demo deployments can start the whole pair without hand-editing
the database.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from core.models import AgentNode, Project, Tenant

CAPABILITIES = ["local.system_info", "local.disk_usage"]


class Command(BaseCommand):
    help = "Enroll this machine as a local runner and print its node id."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Local computer")
        parser.add_argument("--token-file", required=True)
        parser.add_argument("--tenant", default="")
        parser.add_argument("--project", default="")

    def handle(self, *args, **options):
        token_file = Path(options["token_file"]).expanduser().resolve()
        tenant = None
        if options["tenant"]:
            tenant = Tenant.objects.filter(slug=options["tenant"]).first()
        if tenant is None:
            tenant = Tenant.objects.order_by("created_at").first()
        if tenant is None:
            raise CommandError("No tenant exists yet; run the demo seed first.")
        projects = Project.objects.filter(tenant=tenant).order_by("name")
        project = projects.filter(slug=options["project"]).first() if options["project"] else None
        if project is None:
            project = projects.first()
        if project is None:
            raise CommandError("No project exists yet; run the demo seed first.")
        user_model = get_user_model()
        owner = user_model.objects.filter(is_superuser=True).order_by("date_joined").first()
        if owner is None:
            owner = user_model.objects.order_by("date_joined").first()
        if owner is None:
            raise CommandError("No user exists yet; run the demo seed first.")

        token = ""
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            token = secrets.token_urlsafe(36)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")

        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        node = AgentNode.objects.filter(project=project, name=options["name"]).first()
        if node is not None and node.node_type != AgentNode.NodeType.LOCAL_RUNNER:
            raise CommandError(
                f"Node {options['name']} already exists as {node.node_type}; pick another name."
            )
        if node is None:
            try:
                node = AgentNode.objects.create(
                    tenant=tenant,
                    project=project,
                    created_by=owner,
                    name=options["name"],
                    node_type=AgentNode.NodeType.LOCAL_RUNNER,
                    capabilities=CAPABILITIES,
                    labels={"role": "console_host"},
                    credential_hash=digest,
                )
            except IntegrityError as exc:
                raise CommandError(str(exc)) from exc
        elif node.credential_hash != digest:
            node.credential_hash = digest
            node.capabilities = CAPABILITIES
            node.save(update_fields=["credential_hash", "capabilities", "updated_at"])
        self.stdout.write(str(node.id))
