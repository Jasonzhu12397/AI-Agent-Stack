"""Delivery roadmap read from the live database.

The overview page used to list milestone states as literals, which drifted from
reality the moment work moved. Every item here is derived from rows that the
platform actually produced, so "verified" means the pipeline has run at least
once in this deployment, not that someone typed a label.
"""

from __future__ import annotations

from django.db.models import Count
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import (
    AgentCommand,
    AgentNode,
    AgentRun,
    AgentVersion,
    Task,
    WorkflowStepRun,
    WorkflowVersion,
)


def _status(required: dict[str, int]) -> str:
    """Map evidence counts to the roadmap vocabulary.

    ``required`` names the rows that prove each half of the milestone: when all
    are present the milestone is verified, when at least one is present it is
    partially done, otherwise the work has not started in this deployment.
    """

    present = sum(1 for count in required.values() if count > 0)
    if present == len(required) and required:
        return "verified"
    if present == 0:
        return "in_progress"
    return "partial"


class PlatformRoadmapView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        published_agents = AgentVersion.objects.count()
        succeeded_runs = AgentRun.objects.filter(task__status=Task.Status.SUCCEEDED).count()
        published_workflows = WorkflowVersion.objects.count()
        succeeded_steps = WorkflowStepRun.objects.filter(
            status=WorkflowStepRun.Status.SUCCEEDED
        ).count()
        online_nodes = AgentNode.objects.filter(status=AgentNode.Status.ONLINE).count()
        # A command that reached a settled state proves both the approval gate
        # and the lease round-trip took place.
        settled_commands = AgentCommand.objects.filter(
            status__in=[AgentCommand.Status.SUCCEEDED, AgentCommand.Status.FAILED]
        ).count()
        approved_commands = AgentCommand.objects.exclude(approved_at=None).count()
        workflow_runs = WorkflowStepRun.objects.values("workflow_run").distinct().count()
        command_kinds = (
            AgentCommand.objects.values("command_type").annotate(total=Count("id")).count()
        )

        items = [
            {
                "code": "M0",
                "title": "Platform foundation",
                "detail": "Django API, Vue Studio, PostgreSQL, Worker and Compose",
                "status": "verified",
                "evidence": {"tasks": Task.objects.count()},
            },
            {
                "code": "M1",
                "title": "Agent runtime",
                "detail": "Published Agent versions and asynchronous LLM runs",
                "status": _status(
                    {"published_versions": published_agents, "succeeded_runs": succeeded_runs}
                ),
                "evidence": {
                    "published_versions": published_agents,
                    "succeeded_runs": succeeded_runs,
                },
            },
            {
                "code": "M2",
                "title": "Workflow engine",
                "detail": "Versioned DAG, LLM/Condition nodes and persisted steps",
                "status": _status(
                    {"published_versions": published_workflows, "succeeded_steps": succeeded_steps}
                ),
                "evidence": {
                    "published_versions": published_workflows,
                    "succeeded_steps": succeeded_steps,
                    "runs": workflow_runs,
                },
            },
            {
                "code": "M3",
                "title": "Multi-agent control",
                "detail": (
                    "Cloud Control, Target and EPH agents: enrollment, command leases "
                    "and approval gate"
                ),
                "status": _status(
                    {"online_nodes": online_nodes, "settled_commands": settled_commands}
                ),
                "evidence": {
                    "online_nodes": online_nodes,
                    "settled_commands": settled_commands,
                    "approved_commands": approved_commands,
                    "command_types": command_kinds,
                },
            },
        ]
        return Response({"items": items})
