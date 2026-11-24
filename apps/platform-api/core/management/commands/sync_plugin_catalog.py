"""Register the connectors the composer offers in its plugin menu.

The catalog is a data file, not code: every entry names the environment
variable that holds its HTTPS origin, so an operator enables a connector by
configuration alone. A connector whose endpoint is not configured is still
registered, but disabled, so the console can tell the member exactly which
variable is missing instead of hiding the capability.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import AgentPluginDefinition, Tenant

DEFAULT_CATALOG = Path("deploy") / "agent-plugins" / "catalog.json"


def _catalog_file() -> Path:
    """Find the catalog by walking up from the configured base directory."""

    base = Path(getattr(settings, "BASE_DIR", Path.cwd())).resolve()
    for parent in [base, *base.parents]:
        candidate = parent / DEFAULT_CATALOG
        if candidate.exists():
            return candidate
    return base / DEFAULT_CATALOG


def _capability(entry: dict) -> dict:
    # A catalog entry that declares its own input schema and query mapping is
    # callable with real arguments; the generic {"text"} shape stays for
    # connectors whose upstream only accepts a prompt.
    if isinstance(entry.get("input_schema"), dict):
        return {
            "name": entry["name"],
            "description": entry.get("description", entry["name"]),
            "input_schema": entry["input_schema"],
            "output_schema": entry.get("output_schema")
            or {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": True,
            },
            "query_params": entry.get("query_params") or {},
            "risk_level": entry.get("risk_level", "low"),
            "timeout_seconds": entry.get("timeout_seconds", 30),
        }
    return {
        "name": entry["name"],
        "description": entry.get("description", entry["name"]),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        "risk_level": "low",
        "timeout_seconds": 30,
    }


def _manifest(endpoint: str, capabilities: list[dict]) -> dict:
    operations = {
        item["name"]: {
            "method": "GET",
            "path": "/v1/" + item["name"],
            "query_params": item.get("query_params") or {},
        }
        for item in capabilities
    }
    return {
        "http": {"base_url": endpoint, "operations": operations},
        "capabilities": capabilities,
    }


class Command(BaseCommand):
    help = "Upsert the platform connector catalog for tenants."

    def add_arguments(self, parser):
        parser.add_argument("--catalog", default="")
        parser.add_argument("--tenant", default="")

    def handle(self, *args, **options):
        path = Path(
            options["catalog"]
            or getattr(settings, "PLUGIN_CATALOG_PATH", "")
            or _catalog_file()
        )
        if not path.exists():
            raise CommandError(f"catalog file not found: {path}")
        entries = json.loads(path.read_text(encoding="utf-8")).get("plugins", [])
        tenants = Tenant.objects.order_by("created_at")
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])
        created = enabled = 0
        for tenant in tenants:
            owner = tenant.memberships.order_by("created_at").first()
            if owner is None:
                continue
            for entry in entries:
                builtin = bool(entry.get("builtin"))
                endpoint = (os.environ.get(entry.get("endpoint_env", "")) or "").strip()
                capabilities = [_capability(item) for item in entry.get("capabilities", [])]
                definition, _ = AgentPluginDefinition.objects.update_or_create(
                    tenant=tenant,
                    plugin_key=entry["plugin_key"],
                    defaults={
                        "created_by_id": owner.user_id,
                        "name": entry["name"],
                        "description": (
                            entry["description"]
                            if builtin or endpoint
                            else entry["description"]
                            + " Configure "
                            + entry.get("endpoint_env", "")
                            + " to enable it."
                        ),
                        "version": entry.get("version", "1.0.0"),
                        "protocol": entry["protocol"],
                        "endpoint_url": endpoint,
                        "auth_required": bool(entry.get("auth_required")),
                        "capabilities": capabilities,
                        "manifest": (
                            _manifest(endpoint, capabilities)
                            if endpoint and not builtin
                            else {}
                        ),
                        "enabled": builtin or bool(endpoint),
                    },
                )
                created += 1
                enabled += 1 if definition.enabled else 0
        self.stdout.write(f"catalog entries={created} enabled={enabled}")
