"""What a workflow node is allowed to reference, in one call.

Every processing node takes a UUID: a model config, knowledge bases, an
installed connector tool, or a published agent version. Asking an operator to
paste those by hand is the main reason a draft stalls between "I inserted a
node" and "the DAG validates", so the studio gets the identifiers it can
actually choose from, scoped exactly like the server-side validation.
"""

from django.db.models import F
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.knowledge_models import KnowledgeBase
from core.models import AgentVersion, ModelConfig, PluginInstallation, PluginTool
from core.workflow_views import workflow_project, workflow_tenant


def available_tools(tenant, project):
    return (
        PluginTool.objects.select_related("plugin")
        .filter(
            enabled=True,
            plugin__enabled=True,
            plugin__review_status="approved",
            plugin__installations__tenant=tenant,
            plugin__installations__project=project,
            plugin__installations__status=PluginInstallation.Status.INSTALLED,
            plugin__installations__plugin_version=F("plugin__version"),
        )
        .distinct()
        .order_by("name")
    )


class WorkflowCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = workflow_tenant(request, tenant_id)
        project = workflow_project(request, tenant, project_id)
        models = (
            ModelConfig.objects.select_related("provider")
            .filter(provider__tenant=tenant)
            .order_by("display_name")
        )
        knowledge_bases = KnowledgeBase.objects.filter(
            tenant=tenant, project=project, is_archived=False
        ).order_by("name")
        agents = (
            AgentVersion.objects.select_related("agent")
            .filter(agent__tenant=tenant, agent__project=project, agent__is_archived=False)
            .order_by("-published_at")
        )
        return Response(
            {
                "models": [
                    {
                        "id": str(model.id),
                        "label": model.display_name,
                        "provider": model.provider.name,
                        "model_name": model.model_name,
                    }
                    for model in models
                ],
                "knowledge_bases": [
                    {"id": str(item.id), "name": item.name} for item in knowledge_bases
                ],
                "tools": [
                    {
                        "id": str(tool.id),
                        "name": tool.name,
                        "risk_level": tool.risk_level,
                        "plugin": tool.plugin.name,
                    }
                    for tool in available_tools(tenant, project)
                ],
                "agents": [
                    {
                        "id": str(version.id),
                        "name": version.agent.name,
                        "version": version.version,
                    }
                    for version in agents
                ],
            }
        )
