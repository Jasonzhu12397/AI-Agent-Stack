"""URL routes for the marketplace agent binding API.

This module intentionally exposes only ``urlpatterns`` so the platform URL
conf can ``include()`` it without touching the shared ``core/urls.py``.
"""

from django.urls import path

from core.plugin_agent_binding import MarketplaceAgentListView, PluginAgentBindView

urlpatterns = [
    path(
        "api/v1/agent-plugin-installations/<uuid:installation_id>/bind-agent/",
        PluginAgentBindView.as_view(),
        name="agent-plugin-bind-agent",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/marketplace-agents/",
        MarketplaceAgentListView.as_view(),
        name="marketplace-agent-list",
    ),
]
