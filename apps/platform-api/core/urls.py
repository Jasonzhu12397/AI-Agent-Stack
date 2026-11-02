from django.urls import path

from core.views import (
    TenantListView,
    TenantProjectListView,
    liveness,
    readiness,
)

urlpatterns = [
    path("health/live", liveness, name="health-live"),
    path("health/ready", readiness, name="health-ready"),
    path("api/v1/tenants/", TenantListView.as_view(), name="tenant-list"),
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/",
        TenantProjectListView.as_view(),
        name="tenant-project-list",
    ),
]
