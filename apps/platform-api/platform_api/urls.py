from django.contrib import admin
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from packages.version import project_version


@api_view(["GET"])
@permission_classes([AllowAny])
def api_root(request):
    return Response({"name": "AI Agent Stack API", "version": project_version()})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api_root, name="api-root"),
    path("", include("core.urls")),
]
