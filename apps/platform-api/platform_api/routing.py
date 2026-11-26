from core.consumers import ProjectEventConsumer
from django.urls import path

websocket_urlpatterns = [
    path(
        "ws/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/events/",
        ProjectEventConsumer.as_asgi(),
    ),
]
