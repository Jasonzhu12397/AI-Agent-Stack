"""Reusable connector catalogue bootstrap.

The management commands and tenant onboarding share one implementation so a
new workspace sees exactly the same connector registry as an existing one.
The catalog is data: an entry names the environment variable holding its
HTTPS origin, and an entry without an endpoint is registered but disabled so
the console can show which variable is missing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.conf import settings

from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    Project,
    Tenant,
)

DEFAULT_CATALOG = Path("deploy") / "agent-plugins" / "catalog.json"


def catalog_file(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    configured = getattr(settings, "PLUGIN_CATALOG_PATH", "")
    if configured:
        return Path(configured)
    base = Path(getattr(settings, "BASE_DIR", Path.cwd())).resolve()
    for parent in [base, *base.parents]:
        candidate = parent / DEFAULT_CATALOG
        if candidate.exists():
            return candidate
    return base / DEFAULT_CATALOG


def load_catalog_entries(path: str | Path | None = None) -> list[dict]:
    return json.loads(catalog_file(path).read_text(encoding="utf-8")).get("plugins", [])


def _capability(entry: dict) -> dict:
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


def _catalog_metadata(entry: dict, availability: str) -> dict:
    return {
        "kind": entry.get("kind", "connector"),
        "category": entry.get("category", ""),
        "vendor": entry.get("vendor", ""),
        "availability": availability,
        "usage_count": int(entry.get("usage_count", 0)),
        "featured": bool(entry.get("featured")),
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


def sync_tenant_catalog(
    tenant: Tenant,
    *,
    owner_id: int,
    entries: list[dict] | None = None,
) -> tuple[int, int]:
    """Upsert every catalog entry for one tenant and return (registered, enabled)."""

    catalog = entries if entries is not None else load_catalog_entries()
    created = enabled = 0
    for entry in catalog:
        builtin = bool(entry.get("builtin"))
        endpoint = (os.environ.get(entry.get("endpoint_env", "")) or "").strip()
        capabilities = [_capability(item) for item in entry.get("capabilities", [])]
        definition, _ = AgentPluginDefinition.objects.update_or_create(
            tenant=tenant,
            plugin_key=entry["plugin_key"],
            defaults={
                "created_by_id": owner_id,
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
                "manifest": {
                    **(_manifest(endpoint, capabilities) if endpoint and not builtin else {}),
                    "catalog": _catalog_metadata(
                        entry,
                        "builtin" if builtin else ("ready" if endpoint else "unprovisioned"),
                    ),
                },
                "enabled": builtin or bool(endpoint),
            },
        )
        created += 1
        enabled += 1 if definition.enabled else 0
    return created, enabled


def install_builtin_connectors(tenant: Tenant, *, owner_id: int) -> int:
    """Install enabled built-in connectors into every project of one tenant."""

    definitions = AgentPluginDefinition.objects.filter(
        tenant=tenant,
        enabled=True,
        protocol=AgentPluginDefinition.Protocol.OPEN_DATA,
    )
    installed = 0
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
                    "installed_by_id": owner_id,
                    "credential_ref": "",
                },
            )
            if not created and row.status != AgentPluginInstallation.Status.INSTALLED:
                row.status = AgentPluginInstallation.Status.INSTALLED
                row.save(update_fields=["status", "updated_at"])
            installed += 1
    return installed
