from django.contrib import admin
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def api_root(request):
    return Response({"name": "AI Agent Stack API", "version": "0.2.0"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api_root, name="api-root"),
    path("", include("core.urls")),
]
