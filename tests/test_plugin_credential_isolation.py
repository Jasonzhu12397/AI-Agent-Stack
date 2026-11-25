"""A plugin credential UUID is not an authorization token.

Credentials are tenant-owned. Both installation binding and resolution must
reject a reference from another workspace even when its UUID is valid.
"""

import pytest
from core import plugin_credentials, plugin_studio
from core.agent_plugin_views import _credential_reference_error
from core.models import Tenant
from django.contrib.auth import get_user_model


@pytest.mark.django_db
def test_credential_resolution_and_binding_are_tenant_scoped():
    user = get_user_model().objects.create_user(username="credential-owner")
    tenant_a = Tenant.objects.create(name="Credential A", slug="credential-a")
    tenant_b = Tenant.objects.create(name="Credential B", slug="credential-b")
    ref = plugin_studio.seal_credential(
        tenant_a, user, "A secret", {"type": "bearer", "token": "token-a"}
    )

    assert plugin_credentials.resolve(ref, tenant_id=tenant_a.id) == {
        "type": "bearer",
        "token": "token-a",
    }
    with pytest.raises(plugin_credentials.CredentialAccessDeniedError):
        plugin_credentials.resolve(ref, tenant_id=tenant_b.id)

    failure = _credential_reference_error(ref, tenant_b)
    assert failure is not None
    assert failure[1] == "CREDENTIAL_ACCESS_DENIED"
    assert _credential_reference_error(ref, tenant_a) is None
