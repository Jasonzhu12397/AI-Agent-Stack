from django.urls import path

from core.connector_settings_views import (
    ConnectorCatalogView,
    ConnectorHealthView,
    ConnectorInstallationUpdateView,
    ConnectorInstallView,
    ConnectorTestView,
)
from core.data_controls_views import (
    ArchiveItemListView,
    ArchiveItemPurgeView,
    ArchiveItemRestoreView,
    DataExportDetailView,
    DataExportExpireView,
    DataExportListCreateView,
    DeletionRequestListCreateView,
    PrivacyGrantListView,
    PrivacyGrantRevokeView,
    PrivacySessionListView,
)

urlpatterns = [
    # Connectors
    path(
        "api/v1/tenants/<uuid:tenant_id>/connectors/",
        ConnectorCatalogView.as_view(),
        name="connector-catalog-settings",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/connectors/<str:slug>/install/",
        ConnectorInstallView.as_view(),
        name="connector-install-settings",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/installations/<uuid:installation_id>/",
        ConnectorInstallationUpdateView.as_view(),
        name="connector-installation-update",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/installations/<uuid:installation_id>/health/",
        ConnectorHealthView.as_view(),
        name="connector-installation-health",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/installations/<uuid:installation_id>/test/",
        ConnectorTestView.as_view(),
        name="connector-installation-test",
    ),
    # Data exports
    path(
        "api/v1/tenants/<uuid:tenant_id>/data-controls/exports/",
        DataExportListCreateView.as_view(),
        name="data-export-list-create",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/data-controls/exports/<uuid:job_id>/",
        DataExportDetailView.as_view(),
        name="data-export-detail",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/data-controls/exports/<uuid:job_id>/expire/",
        DataExportExpireView.as_view(),
        name="data-export-expire",
    ),
    # Archived content
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/archive-items/",
        ArchiveItemListView.as_view(),
        name="archive-item-list",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/archive-items/<str:item_type>/<uuid:item_id>/restore/",
        ArchiveItemRestoreView.as_view(),
        name="archive-item-restore",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/archive-items/<str:item_type>/<uuid:item_id>/",
        ArchiveItemPurgeView.as_view(),
        name="archive-item-purge",
    ),
    # Privacy
    path(
        "api/v1/tenants/<uuid:tenant_id>/privacy/grants/",
        PrivacyGrantListView.as_view(),
        name="privacy-grant-list",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/privacy/grants/<uuid:grant_id>/",
        PrivacyGrantRevokeView.as_view(),
        name="privacy-grant-revoke",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/privacy/sessions/",
        PrivacySessionListView.as_view(),
        name="privacy-session-list",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/privacy/deletion-requests/",
        DeletionRequestListCreateView.as_view(),
        name="privacy-deletion-request-list-create",
    ),
]
