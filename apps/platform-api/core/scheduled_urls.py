from django.urls import path

from core.chat_views import (
    AgentChatAttachmentDetailView,
    AgentChatAttachmentListCreateView,
)
from core.scheduled_views import (
    ScheduledJobDetailView,
    ScheduledJobListCreateView,
    ScheduledJobRunNowView,
)

JOBS = "api/v1/tenants/<uuid:tenant_id>/projects/<uuid:project_id>/scheduled-jobs/"

urlpatterns = [
    path(JOBS, ScheduledJobListCreateView.as_view(), name="scheduled-jobs"),
    path(JOBS + "<uuid:job_id>/", ScheduledJobDetailView.as_view(), name="scheduled-job-detail"),
    path(
        JOBS + "<uuid:job_id>/run-now/",
        ScheduledJobRunNowView.as_view(),
        name="scheduled-job-run-now",
    ),
    path(
        "api/v1/agent-chat/threads/<uuid:thread_id>/attachments/",
        AgentChatAttachmentListCreateView.as_view(),
        name="agent-chat-attachments",
    ),
    path(
        "api/v1/agent-chat/attachments/<uuid:attachment_id>/",
        AgentChatAttachmentDetailView.as_view(),
        name="agent-chat-attachment-detail",
    ),
]
