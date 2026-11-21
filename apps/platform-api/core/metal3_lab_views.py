"""Read-only endpoints describing lab infrastructure this platform can verify."""

from __future__ import annotations

import os

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.lab_workflow import lab_workflow_report


class Metal3LabWorkflowView(APIView):
    """Report the physical lab workflow's shape to the UI.

    The platform cannot read GitHub secrets, so this endpoint exposes only what
    the checked-in workflow file proves and says so explicitly
    (``secret_presence: not_observable``) instead of rendering a green tag that
    would imply a lab run had succeeded.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        report = lab_workflow_report(settings.BASE_DIR)
        workflow_url = os.environ.get("METAL3_LAB_WORKFLOW_URL", "").strip()
        report["workflow_url"] = workflow_url or None
        return Response(report)
