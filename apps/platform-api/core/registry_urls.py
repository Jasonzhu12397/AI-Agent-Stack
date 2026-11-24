from django.urls import path

from core.registry_views import (
    RegistryImageDetailView,
    RegistryImageEventListView,
    RegistryImageListCreateView,
    RegistryImagePromoteView,
    RegistryImageScanView,
    RegistryImageSignView,
    RegistryPullPlanView,
    RegistrySummaryView,
)

BASE = "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/registry"
IMAGES = BASE + "/images/"

urlpatterns = [
    path(IMAGES, RegistryImageListCreateView.as_view(), name="registry-images"),
    path(f"{BASE}/summary/", RegistrySummaryView.as_view(), name="registry-summary"),
    path(f"{BASE}/pull-plan/", RegistryPullPlanView.as_view(), name="registry-pull-plan"),
    path(
        IMAGES + "<uuid:image_id>/",
        RegistryImageDetailView.as_view(),
        name="registry-image-detail",
    ),
    path(
        IMAGES + "<uuid:image_id>/scan/",
        RegistryImageScanView.as_view(),
        name="registry-image-scan",
    ),
    path(
        IMAGES + "<uuid:image_id>/sign/",
        RegistryImageSignView.as_view(),
        name="registry-image-sign",
    ),
    path(
        IMAGES + "<uuid:image_id>/promote/",
        RegistryImagePromoteView.as_view(),
        name="registry-image-promote",
    ),
    path(
        IMAGES + "<uuid:image_id>/events/",
        RegistryImageEventListView.as_view(),
        name="registry-image-events",
    ),
]
