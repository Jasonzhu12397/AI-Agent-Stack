from django.urls import path

from core.membership_views import (
    ProjectMemberDetailView,
    ProjectMemberListCreateView,
    TenantMemberDetailView,
    TenantMemberListCreateView,
)
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
    path(
        "api/v1/tenants/<uuid:tenant_id>/members/",
        TenantMemberListCreateView.as_view(),
        name="tenant-member-list-create",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/members/<uuid:membership_id>/",
        TenantMemberDetailView.as_view(),
        name="tenant-member-detail",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/members/",
        ProjectMemberListCreateView.as_view(),
        name="project-member-list-create",
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/members/<uuid:membership_id>/",
        ProjectMemberDetailView.as_view(),
        name="project-member-detail",
    ),
]
