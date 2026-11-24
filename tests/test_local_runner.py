"""Local execution and the connector catalog the composer offers."""

import threading
import time
from io import StringIO

import pytest
from core import chat_tools
from core.chat_models import AgentChatThread
from core.models import (
    AgentCommand,
    AgentNode,
    AgentPluginDefinition,
    ModelConfig,
    ModelProvider,
    Project,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone


@pytest.fixture
def scope(db):
    user = get_user_model().objects.create_user(username="local-owner")
    tenant = Tenant.objects.create(name="Local tenant", slug="local-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Local project", slug="local-project")
    thread = AgentChatThread.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        execution_target="local",
        model_config=ModelConfig.objects.create(
            provider=ModelProvider.objects.create(
                tenant=tenant,
                created_by=user,
                name="Local provider",
                provider_type=ModelProvider.ProviderType.FAKE,
                config={"response": "ok"},
            ),
            model_name="local-fake",
            display_name="Local Fake",
            capabilities=["chat", "tools"],
            max_output_tokens=1024,
        ),
    )
    return {"user": user, "tenant": tenant, "project": project, "thread": thread}


def _runner(scope, name="Local computer", online=True):
    return AgentNode.objects.create(
        tenant=scope["tenant"],
        project=scope["project"],
        created_by=scope["user"],
        name=name,
        node_type=AgentNode.NodeType.LOCAL_RUNNER,
        capabilities=["local.system_info", "local.disk_usage"],
        credential_hash="y" * 64,
        status=AgentNode.Status.ONLINE if online else AgentNode.Status.OFFLINE,
        last_seen_at=timezone.now(),
    )


def test_local_tools_only_ship_for_a_local_thread(scope):
    scope["thread"].execution_target = "cloud"
    scope["thread"].save(update_fields=["execution_target"])
    names = {item.name for item in chat_tools.build_tools(scope["thread"], {})}
    assert "local_system_info" not in names

    scope["thread"].execution_target = "local"
    scope["thread"].save(update_fields=["execution_target"])
    names = {item.name for item in chat_tools.build_tools(scope["thread"], {})}
    assert {"local_system_info", "local_disk_usage"} <= names


def test_local_tool_fails_clearly_without_a_runner(scope):
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "local_system_info")
    payload = chat_tools.run_tool(tool, {})
    assert payload["ok"] is False
    assert payload["code"] == "LOCAL_RUNNER_OFFLINE"


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("scope")
def test_local_tool_runs_through_the_runner_lease(scope):
    node = _runner(scope)
    # The runner answers what the platform queues for it; the console waits
    # for the same result the agent would report over the runtime protocol.
    def complete():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            command = AgentCommand.objects.filter(node=node).first()
            if command is not None:
                command.status = AgentCommand.Status.SUCCEEDED
                command.result = {"hostname": "console-host", "cpu_count": 8}
                command.save(update_fields=["status", "result", "updated_at"])
                return
            time.sleep(0.05)
        raise AssertionError("the console never queued a local command")

    worker = threading.Thread(target=complete)
    worker.start()
    tools = chat_tools.build_tools(scope["thread"], {})
    tool = next(item for item in tools if item.name == "local_system_info")
    payload = chat_tools.run_tool(tool, {})
    worker.join()
    assert payload["ok"] is True, payload
    assert payload["result"]["hostname"] == "console-host"
    command = AgentCommand.objects.get(node=node)
    assert command.command_type == "local.system_info"
    assert command.status == AgentCommand.Status.SUCCEEDED


def test_catalog_registers_every_connector_even_when_disabled(scope, monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        """{"plugins": [
      {"plugin_key": "map", "name": "地图", "description": "Map connector",
       "protocol": "http_json", "endpoint_env": "MAP_CONNECTOR_URL",
       "capabilities": [{"name": "map.place.search", "description": "Search"}]},
      {"plugin_key": "weather", "name": "天气", "description": "Weather connector",
       "protocol": "http_json", "endpoint_env": "WEATHER_CONNECTOR_URL",
       "capabilities": [{"name": "weather.current", "description": "Now"}]}
    ]}""",
        encoding="utf-8",
    )
    monkeypatch.delenv("MAP_CONNECTOR_URL", raising=False)
    monkeypatch.setenv("WEATHER_CONNECTOR_URL", "https://weather.example.com")
    call_command(
        "sync_plugin_catalog",
        catalog=str(catalog),
        tenant=scope["tenant"].slug,
        stdout=StringIO(),
    )

    map_plugin = AgentPluginDefinition.objects.get(tenant=scope["tenant"], plugin_key="map")
    weather = AgentPluginDefinition.objects.get(tenant=scope["tenant"], plugin_key="weather")
    assert map_plugin.enabled is False
    assert "MAP_CONNECTOR_URL" in map_plugin.description
    assert weather.enabled is True
    operations = weather.manifest["http"]["operations"]
    assert operations["weather.current"]["path"] == "/v1/weather.current"
    assert weather.capabilities[0]["input_schema"]["additionalProperties"] is False
