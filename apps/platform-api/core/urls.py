from django.urls import path

from core.auth_views import CurrentUserView, LoginView, LogoutView, csrf_cookie
from core.membership_views import (
    ProjectMemberDetailView,
    ProjectMemberListCreateView,
    TenantMemberDetailView,
    TenantMemberListCreateView,
)
from core.model_views import ModelConfigListCreateView, ModelProviderListCreateView, ModelTestView
from core.task_views import TaskCancelView, TaskDetailView, TaskListCreateView
from core.views import (
    TenantListView,
    TenantProjectListView,
    liveness,
    readiness,
)

urlpatterns = [
    path(\"api/v1/tenants/<uuid:tenant_id>/model-providers/\", ModelProviderListCreateView.as_view(), name=\"model-provider-list-create\"),
    path(\"api/v1/tenants/<uuid:tenant_id>/model-providers/<uuid:provider_id>/models/\", ModelConfigListCreateView.as_view(), name=\"model-config-list-create\"),
    path(\"api/v1/tenants/<uuid:tenant_id>/model-configs/<uuid:model_config_id>/test/\", ModelTestView.as_view(), name=\"model-test\"),
    path("api/v1/auth/csrf/", csrf_cookie, name="auth-csrf"),
    path("api/v1/auth/login/", LoginView.as_view(), name="auth-login"),
    path("api/v1/auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("api/v1/auth/me/", CurrentUserView.as_view(), name="auth-me"),
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
    path(
        "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/tasks/",
        TaskListCreateView.as_view(),
        name="task-list-create",
    ),
    path("api/v1/tasks/<uuid:task_id>/", TaskDetailView.as_view(), name="task-detail"),
    path(
        "api/v1/tasks/<uuid:task_id>/cancel/",
        TaskCancelView.as_view(),
        name="task-cancel",
    ),
]
