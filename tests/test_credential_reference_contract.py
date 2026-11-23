"""One credential-reference syntax for every entry point.

Two validators used to disagree: the plugin installation API matched its own
regular expression (which rejected ``vault://mount/path#field`` and capped the
path at 200 characters), while the resolver accepted that form. The API could
therefore refuse a reference the resolver would happily resolve.

The contract is now single-sourced:

* syntax is decided by ``core.plugin_credentials.validate_reference``;
* the storage bound is read from the ``credential_ref`` column, so a reference
  that SQLite accepts cannot overflow Postgres;
* both the install endpoint and the rotation endpoint go through the same check.
"""

import pytest
from core.models import (
    AgentPluginInstallation,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from core.plugin_credentials import CredentialReferenceInvalidError, validate_reference
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

SUMMARY_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
SUMMARY_OUTPUT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

COLUMN_LIMIT = AgentPluginInstallation._meta.get_field("credential_ref").max_length


def _manifest():
    return {
        "http": {
            "base_url": "https://contract-agent.example.com",
            "operations": {
                "summarize": {
                    "method": "GET",
                    "path": "/v1/summarize",
                    "query_params": {"text": "text"},
                }
            },
        },
        "capabilities": [
            {
                "name": "summarize",
                "description": "Summarize input text",
                "input_schema": SUMMARY_INPUT,
                "output_schema": SUMMARY_OUTPUT,
                "risk_level": "low",
                "timeout_seconds": 5,
            }
        ],
    }


@pytest.fixture
def contract_workspace(db):
    user = get_user_model().objects.create_user(username="contract-owner")
    tenant = Tenant.objects.create(name="Contract tenant", slug="contract-tenant")
    TenantMembership.objects.create(
        tenant=tenant, user=user, role=TenantMembership.Role.OWNER
    )
    project = Project.objects.create(
        tenant=tenant, name="Contract project", slug="contract-project"
    )
    ProjectMembership.objects.create(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return tenant, project, client


def _register(client, tenant):
    response = client.post(
        f"/api/v1/tenants/{tenant.id}/agent-plugins/",
        {
            "plugin_key": "contract-agent",
            "name": "Contract Agent",
            "version": "1.0.0",
            "protocol": "http_json",
            "endpoint_url": "https://contract-agent.example.com/v1/invoke",
            "auth_required": True,
            "manifest": _manifest(),
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    return response.data["id"]


def _install(client, tenant, project, plugin_id, credential_ref):
    return client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/agent-plugin-installations/",
        {
            "plugin_id": plugin_id,
            "granted_capabilities": ["summarize"],
            "credential_ref": credential_ref,
        },
        format="json",
    )


def test_resolver_accepts_the_documented_vault_field_form():
    assert validate_reference("vault://merchant/rotate-agent#token") == "vault"


@pytest.mark.parametrize(
    "reference",
    [
        "plain-token-value",
        "sk-live-abcdef",
        "",
        "://missing-scheme",
    ],
)
def test_resolver_rejects_shapes_that_are_not_references(reference):
    with pytest.raises(CredentialReferenceInvalidError):
        validate_reference(reference)


@pytest.mark.django_db
def test_install_accepts_the_vault_field_form(contract_workspace):
    tenant, project, client = contract_workspace
    plugin_id = _register(client, tenant)

    response = _install(client, tenant, project, plugin_id, "vault://merchant/contract#token")

    assert response.status_code in {200, 201}, response.data
    stored = AgentPluginInstallation.objects.get(pk=response.data["id"])
    assert stored.credential_ref == "vault://merchant/contract#token"


@pytest.mark.django_db
def test_install_accepts_a_reference_of_exactly_the_column_length(contract_workspace):
    tenant, project, client = contract_workspace
    plugin_id = _register(client, tenant)
    reference = "env://" + "A" * (COLUMN_LIMIT - len("env://"))
    assert len(reference) == COLUMN_LIMIT

    response = _install(client, tenant, project, plugin_id, reference)

    assert response.status_code in {200, 201}, response.data


@pytest.mark.django_db
def test_install_rejects_a_reference_one_character_past_the_column(contract_workspace):
    tenant, project, client = contract_workspace
    plugin_id = _register(client, tenant)
    reference = "env://" + "A" * (COLUMN_LIMIT - len("env://") + 1)

    response = _install(client, tenant, project, plugin_id, reference)

    assert response.status_code == 400
    assert response.data["code"] == "CREDENTIAL_REFERENCE_INVALID"


@pytest.mark.django_db
def test_rotation_applies_the_same_bound_and_syntax(contract_workspace):
    tenant, project, client = contract_workspace
    plugin_id = _register(client, tenant)
    installed = _install(
        client, tenant, project, plugin_id, "env://PLUGIN_SECRET_CONTRACT"
    ).data
    url = f"/api/v1/agent-plugin-installations/{installed['id']}/credential/"

    too_long = client.post(
        url,
        {"credential_ref": "env://" + "A" * (COLUMN_LIMIT - len("env://") + 1)},
        format="json",
    )
    assert too_long.status_code == 400
    assert too_long.data["code"] == "CREDENTIAL_REFERENCE_INVALID"

    with_field = client.post(
        url,
        {"credential_ref": "vault://merchant/contract#token"},
        format="json",
    )
    # The scheme is known, so the reference is resolved: without a configured
    # vault the rotation is refused, but not for a syntax reason.
    assert with_field.status_code == 400
    assert with_field.data["code"] != "CREDENTIAL_REFERENCE_INVALID"
