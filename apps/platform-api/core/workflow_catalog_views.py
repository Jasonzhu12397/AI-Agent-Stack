"""What a workflow node is allowed to reference, in one call.

Every processing node takes a UUID: a model config, knowledge bases, an
installed connector tool, or a published agent version. Asking an operator to
paste those by hand is the main reason a draft stalls between "I inserted a
node" and "the DAG validates", so the studio gets the identifiers it can
actually choose from, scoped exactly like the server-side validation.
"""

from django.db import models
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import provider_accounts
from core.agent_plugin_manifest import capability_definition
from core.knowledge_models import KnowledgeBase
from core.models import AgentPluginInstallation, AgentVersion, ModelConfig
from core.workflow_views import workflow_project, workflow_tenant


def available_tools(tenant, project):
    """Installed capabilities a workflow tool node may bind by name."""
    items = []
    installations = (
        AgentPluginInstallation.objects.select_related("plugin")
        .filter(
            tenant=tenant,
            project=project,
            status=AgentPluginInstallation.Status.INSTALLED,
            plugin__enabled=True,
            plugin_version=models.F("plugin__version"),
        )
        .order_by("plugin__name")
    )
    for installation in installations:
        granted = installation.granted_capabilities or []
        for name in installation.plugin.capabilities or []:
            if name not in granted:
                continue
            definition = capability_definition(installation.plugin, name)
            if definition is None:
                continue
            items.append(
                {
                    "id": name,
                    "name": name,
                    "risk_level": definition.get("risk_level", "low"),
                    "plugin": installation.plugin.plugin_key,
                }
            )
    return items


class WorkflowCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant = workflow_tenant(request, tenant_id)
        project = workflow_project(request, tenant, project_id)
        models = (
            ModelConfig.objects.select_related("provider")
            .filter(
                provider_accounts.model_config_scope_q(tenant, request.user),
                provider__enabled=True,
                enabled=True,
            )
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
                "tools": available_tools(tenant, project),
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
