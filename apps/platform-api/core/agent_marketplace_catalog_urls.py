# ruff: noqa: I001
"""URL routes for the read-only agent marketplace catalog.

This module exposes a standalone ``urlpatterns`` list that the platform URL
configuration includes; the catalog routes are kept separate so the marketplace
plug-in can be wired in without touching the core URL table.
"""

from django.urls import path

from core.agent_marketplace_catalog import (
    AgentMarketplaceCatalogDetailView,
    AgentMarketplaceCatalogView,
)

urlpatterns = [
    path(
        "api/v1/tenants/<uuid:tenant_id>/marketplace/catalog/",
        AgentMarketplaceCatalogView.as_view(),
        name="agent-marketplace-catalog",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/marketplace/catalog/<slug:plugin_key>/",
        AgentMarketplaceCatalogDetailView.as_view(),
        name="agent-marketplace-catalog-detail",
    ),
]
