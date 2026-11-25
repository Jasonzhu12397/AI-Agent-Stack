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


def _thread(client, tenant, project, model=None, title="Fleet check"):
    payload = {"title": title} if title is not None else {}
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
def test_first_question_names_an_untitled_thread(chat_workspace):
    user, tenant, project, model, _installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, model, title=None)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "Which plugins are installed in AI-Agent-Stack?"},
        format="json",
    )
    assert response.status_code == 201, response.content
    assert AgentChatThread.objects.get(pk=thread["id"]).title == (
        "Which plugins are installed in AI-Agent-Stack?"
    )


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


@pytest.mark.django_db
def test_context_reports_whether_a_plugin_still_needs_authorization(chat_workspace):
    """The composer disables an unauthorized connector and offers the authorize jump."""

    user, tenant, project, _model, installation = chat_workspace
    client = APIClient()
    client.force_authenticate(user=user)
    url = f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/"

    unauthorized = client.get(url)
    assert unauthorized.status_code == 200, unauthorized.content
    row = unauthorized.json()["plugins"][0]
    assert row["auth_required"] is True
    assert row["credential_configured"] is False

    installation.credential_ref = "env://PLUGIN_SECRET_OPS_AGENT_TOKEN"
    installation.save(update_fields=["credential_ref"])
    authorized = client.get(url).json()["plugins"][0]
    assert authorized["credential_configured"] is True


@pytest.mark.django_db
def test_turn_falls_back_to_another_real_model_when_selected_key_is_rejected(
    chat_workspace, monkeypatch
):
    user, tenant, project, fake_model, _installation = chat_workspace
    bad_provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="OpenAI (my account)",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        auth_mode=ModelProvider.AuthMode.TENANT_SECRET,
        base_url="https://api.openai.com/v1",
        credential_ref="env:OPENAI_API_KEY",
    )
    bad_model = ModelConfig.objects.create(
        provider=bad_provider,
        model_name="gpt-5.6",
        display_name="GPT-5.6",
        capabilities=["chat"],
        max_output_tokens=1024,
    )
    backup_provider = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="Backup OpenAI",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        auth_mode=ModelProvider.AuthMode.TENANT_SECRET,
        base_url="https://backup.example.com/v1",
        credential_ref="env:BACKUP_OPENAI_KEY",
    )
    ModelConfig.objects.create(
        provider=backup_provider,
        model_name="gpt-5.6-backup",
        display_name="GPT-5.6 Backup",
        capabilities=["chat"],
        max_output_tokens=1024,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, bad_model)
    from core import chat_runtime
    from core.chat_views import _failure  # noqa: F401

    def fake_run(thread_arg, model_arg, **kwargs):
        if model_arg.id == bad_model.id:
            raise chat_runtime.ChatRuntimeError(
                "MODEL_CREDENTIAL_REJECTED",
                "OpenAI rejected the account credential (HTTP 401).",
                403,
            )
        return ("Fallback answered.", {"input_tokens": 3, "output_tokens": 4}, [])

    monkeypatch.setattr(chat_runtime, "run_turn", fake_run)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "hello"},
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["assistant"]["content"] == "Fallback answered."
    assert body["assistant"]["usage"]["fallback_provider"] == "Backup OpenAI"
    assert body["assistant"]["usage"]["fallback_reason"] == "MODEL_CREDENTIAL_REJECTED"


@pytest.mark.django_db
def test_linked_personal_model_is_recommended_over_the_local_echo(chat_workspace):
    user, tenant, project, fake_model, _installation = chat_workspace
    from core.models import ProviderAccountLink

    personal = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="OpenAI (my account)",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        auth_mode=ModelProvider.AuthMode.USER_ACCOUNT,
        preset_key="openai",
        base_url="https://api.openai.com/v1",
    )
    personal_model = ModelConfig.objects.create(
        provider=personal,
        model_name="gpt-5.6",
        display_name="GPT-5.6",
        capabilities=["chat", "tools"],
    )
    ProviderAccountLink.objects.create(
        user=user,
        provider=personal,
        credential_ref="env:OPENAI_API_KEY",
        status=ProviderAccountLink.Status.LINKED,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    context = client.get(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-chat/context/"
    ).json()
    by_id = {row["id"]: row for row in context["models"]}
    assert by_id[str(personal_model.id)]["recommended"] is True
    # The local echo provider disappears once a real provider is configured,
    # so the picker cannot present a fake model as if it were the linked one.
    assert str(fake_model.id) not in by_id

    thread = _thread(client, tenant, project)
    assert thread["model_config_id"] == str(personal_model.id)


@pytest.mark.django_db
def test_stale_demo_model_id_is_replaced_by_a_real_model(chat_workspace, monkeypatch):
    """A tab holding the old demo id must not bypass the picker's real models."""

    from core import chat_runtime, provider_accounts

    user, tenant, project, fake_model, _installation = chat_workspace
    personal = ModelProvider.objects.create(
        tenant=tenant,
        created_by=user,
        name="OpenAI (my account)",
        provider_type=ModelProvider.ProviderType.OPENAI_COMPATIBLE,
        auth_mode=ModelProvider.AuthMode.USER_ACCOUNT,
        preset_key="openai",
        base_url="https://api.openai.com/v1",
    )
    real_model = ModelConfig.objects.create(
        provider=personal,
        model_name="gpt-5.6",
        display_name="GPT-5.6",
        capabilities=["chat"],
    )
    provider_accounts.link_account(
        user=user, provider=personal, credential_ref="env:OPENAI_TEST_KEY"
    )
    client = APIClient()
    client.force_authenticate(user=user)
    thread = _thread(client, tenant, project, fake_model)
    seen = {}

    def fake_run(thread_arg, model_config, **kwargs):
        seen["model_id"] = str(model_config.id)
        return "A real answer", {}, []

    monkeypatch.setattr(chat_runtime, "run_turn", fake_run)
    response = client.post(
        f"/api/v1/agent-chat/threads/{thread['id']}/messages/",
        {"content": "你好", "model_config_id": str(fake_model.id)},
        format="json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["assistant"]["content"] == "A real answer"
    assert seen["model_id"] == str(real_model.id)
