from types import SimpleNamespace
from unittest.mock import patch

import pytest
from core.models import (
    Project,
    Tenant,
)
from django.contrib.auth import get_user_model
from worker_app import tasks as worker_tasks


def test_provider_token_overrun_is_permanent_and_never_retried():
    reservation = SimpleNamespace(reserved_tokens=40)
    with patch.object(
        worker_tasks,
        "settle_tenant_tokens",
        side_effect=ValueError("Consumed tokens exceed the reserved provider-call ceiling"),
    ):
        with pytest.raises(
            worker_tasks.PermanentTaskError,
            match="automatic retry is unsafe",
        ):
            worker_tasks._settle_provider_usage(reservation, 41)



def test_invalid_provider_token_usage_is_permanent_and_never_retried():
    with patch.object(
        worker_tasks,
        "resolve_provider_token_usage",
        side_effect=ValueError("Provider token usage must be non-negative integers"),
    ):
        with pytest.raises(
            worker_tasks.PermanentTaskError,
            match="automatic retry is unsafe",
        ):
            worker_tasks._resolve_provider_usage(
                provider_type="openai_compatible",
                reported_input_tokens=-1,
                reported_output_tokens=0,
                input_token_ceiling=40,
                output_token_ceiling=20,
            )


@pytest.mark.django_db
def test_worker_loads_capability_tools_from_installed_connector():
    from core.models import AgentPluginDefinition, AgentPluginInstallation

    user = get_user_model().objects.create_user(username="capability-agent-user")
    tenant = Tenant.objects.create(name="Capability tenant", slug="capability-tenant")
    project = Project.objects.create(
        tenant=tenant, name="Capability project", slug="capability-project"
    )
    definition = AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="weather-agent",
        name="Weather Agent",
        description="Built-in weather",
        version="1.0.0",
        protocol=AgentPluginDefinition.Protocol.OPEN_DATA,
        endpoint_url="",
        auth_required=False,
        capabilities=[
            {
                "name": "weather.current",
                "description": "Current conditions",
                "risk_level": "low",
                "input_schema": {
                    "type": "object",
                    "properties": {"place": {"type": "string"}},
                    "required": ["place"],
                },
                "output_schema": {"type": "object", "properties": {}},
            }
        ],
        enabled=True,
    )
    AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=definition,
        plugin_version=definition.version,
        granted_capabilities=["weather.current"],
        installed_by=user,
    )
    task = SimpleNamespace(tenant_id=tenant.id, project_id=project.id)

    offered, by_name = worker_tasks._load_agent_capabilities(
        task, {"capability_allowlist": ["weather.current"]}
    )

    assert len(offered) == 1
    function = offered[0]["function"]
    assert function["name"] == "weather-agent__weather_current"
    assert function["parameters"]["additionalProperties"] is False
    assert by_name[function["name"]].capability == "weather.current"
