"""Control Agent chat: threads, composer context and the reply path."""

import pytest
from core.chat_models import AgentChatMessage, AgentChatThread
from core.models import (
    AgentPluginDefinition,
    AgentPluginInstallation,
    ModelConfig,
    ModelProvider,
    Project,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def chat_workspace(db):
    user = get_user_model().objects.create_user(username="chat-owner", password="pw-chat-1234")
    tenant = Tenant.objects.create(name="Chat tenant", slug="chat-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="member")
    project = Project.objects.create(tenant=tenant, name="Chat project", slug="chat-project")
    provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Chat fake provider",
        provider_type=ModelProvider.ProviderType.FAKE,
        config={"response": "Control Agent reply"},
    )
    model = ModelConfig.objects.create(
        provider=provider,
        model_name="chat-fake",
        display_name="Chat Fake",
        capabilities=["chat"],
        max_output_tokens=1024,
    )
    definition = AgentPluginDefinition.objects.create(
        tenant=tenant,
        created_by=user,
        plugin_key="ops-agent",
        name="Operations agent",
        version="1.0.0",
        protocol=AgentPluginDefinition.Protocol.HTTP_JSON,
        endpoint_url="https://ops.example.com/hooks",
        capabilities=["fleet.inspect"],
    )
    installation = AgentPluginInstallation.objects.create(
        tenant=tenant,
        project=project,
        plugin=definition,
        plugin_version="1.0.0",
        granted_capabilities=["fleet.inspect"],
        installed_by=user,
    )
    return user, tenant, project, model, installation


def _thread(client, tenant, project, model=None):
    payload = {"title": "Fleet check"}
    if model is not None:
        payload["model_config_id"] = str(model.id)
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/threads/",
        payload,
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()


@pytest.mark.django_db
def test_thread_creation_and_turn_produce_assistant_reply(chat_workspace):
    user, tenant, project, model, _installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, model)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "Which machines are pending enrollment?"},
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["user"]["role"] == "user"
    assert body["assistant"]["role"] == "assistant"
    assert body["assistant"]["content"] == "Control Agent reply"
    messages = AgentChatMessage.objects.filter(thread_id=thread["id"]).order_by("created_at")
    assert [message.role for message in messages] == ["user", "assistant"]
    assert AgentChatThread.objects.get(pk=thread["id"]).title == "Fleet check"


@pytest.mark.django_db
def test_context_endpoint_lists_projects_skills_plugins_and_models(chat_workspace):
    user, tenant, project, model, installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/")
    assert response.status_code == 200
    body = response.json()
    assert body["project"]["name"] == "Chat project"
    assert [item["id"] for item in body["projects"]] == [str(project.id)]
    assert body["skills"][0]["id"] == "fleet.inspect"
    assert body["plugins"][0]["id"] == str(installation.id)
    assert body["models"][0]["id"] == str(model.id)
    assert body["models"][0]["auth_mode"] == ModelProvider.AuthMode.TENANT_SECRET


@pytest.mark.django_db
def test_unknown_skill_in_composer_context_is_rejected(chat_workspace):
    user, tenant, project, model, _installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, model)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {
            "content": "hello",
            "context": {"skills": ["skill.that.does.not.exist"], "plugins": []},
        },
        format="json",
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_CONTEXT"
    assert AgentChatMessage.objects.filter(thread_id=thread["id"]).count() == 0


@pytest.mark.django_db
def test_attached_skill_and_plugin_are_stored_on_the_turn(chat_workspace):
    user, tenant, project, model, installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, model)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {
            "content": "Run an inspection",
            "context": {"skills": ["fleet.inspect"], "plugins": [str(installation.id)]},
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    user_message = AgentChatMessage.objects.get(thread_id=thread["id"], role="user")
    assert user_message.context["skills"] == ["fleet.inspect"]
    assert user_message.context["plugins"] == [str(installation.id)]


@pytest.mark.django_db
def test_tenant_without_any_model_returns_configuration_error(db):
    user = get_user_model().objects.create_user(username="no-model", password="pw-no-model")
    tenant = Tenant.objects.create(name="No model tenant", slug="no-model-tenant")
    TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    project = Project.objects.create(tenant=tenant, name="Bare", slug="bare")
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "hello"},
        format="json",
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"


@pytest.mark.django_db
def test_thread_without_a_selection_uses_the_tenant_default_model(chat_workspace):
    user, tenant, project, model, _installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "hello"},
        format="json",
    )
    assert response.status_code == 201, response.content
    assert AgentChatThread.objects.get(pk=thread["id"]).model_config_id == model.id


@pytest.mark.django_db
def test_other_tenant_cannot_read_a_thread(chat_workspace):
    user, tenant, project, model, _installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, model)
    outsider = get_user_model().objects.create_user(username="outsider", password="pw-outsider")
    other = APIClient()
    other.force_authenticate(user=outsider)
    assert other.get(f"/api/v1/agent-chat/threads/{thread['id']}/").status_code == 404
