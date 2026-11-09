# ruff: noqa: I001
import httpx
import pytest

from core.bmc_credentials import (
    _parse_vault_reference,
    _tenant_scoped_path,
    resolve_bmc_credentials,
)


def test_parse_vault_reference_requires_mount_and_safe_path():
    assert _parse_vault_reference("vault://secret/bmc/rack-a") == (
        "secret",
        "bmc/rack-a",
    )


@pytest.mark.parametrize(
    "reference",
    [
        "http://vault/secret/key",
        "vault:///secret/key",
        "vault://secret/../admin",
        "vault://secret/%2e%2e/admin",
        "vault://user:pass@secret/path",
        "vault://secret/path?version=1",
        "vault://secret/path#fragment",
    ],
)
def test_parse_vault_reference_rejects_unsafe_values(reference):
    with pytest.raises(ValueError):
        _parse_vault_reference(reference)


@pytest.mark.parametrize(
    ("tenant_id", "project_id"),
    [
        ("", "project-id"),
        ("tenant-id", ""),
        ("../tenant", "project-id"),
        ("tenant-id", "project/other"),
    ],
)
def test_vault_resolver_requires_valid_scope_before_network(
    settings, monkeypatch, tenant_id, project_id
):
    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted without a valid scope.")

    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(httpx, "Client", unexpected_client)

    with pytest.raises(ValueError, match="scope"):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id=tenant_id,
            project_id=project_id,
        )


def test_vault_paths_are_isolated_by_tenant_and_project():
    path_a = _tenant_scoped_path("bmc/rack-a", "tenant-a", "project-a")
    path_b = _tenant_scoped_path("bmc/rack-a", "tenant-b", "project-a")
    path_c = _tenant_scoped_path("bmc/rack-a", "tenant-a", "project-b")

    assert path_a == "tenants/tenant-a/projects/project-a/bmc/rack-a"
    assert len({path_a, path_b, path_c}) == 3


def test_vault_resolver_fails_closed_without_token(settings):
    settings.BMC_VAULT_TOKEN = ""
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    with pytest.raises(RuntimeError, match="not configured"):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id="tenant-id",
            project_id="project-id",
        )


def test_vault_resolver_requires_https(settings):
    settings.BMC_VAULT_TOKEN = "test-token"
    settings.BMC_VAULT_ADDR = "http://vault.example.net"
    with pytest.raises(ValueError, match="HTTPS"):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id="tenant-id",
            project_id="project-id",
        )


def test_vault_resolver_reads_kv_v2_credentials_and_trust_bundle(
    settings, monkeypatch
):
    import core.bmc_credentials as vault_module
    import httpx

    requests = []
    original_client = httpx.Client

    def handler(request):
        requests.append(request)
        if request.url.path == (
            "/v1/secret/data/tenants/tenant-id/projects/project-id/bmc/rack-a"
        ):
            payload = {
                "data": {
                    "data": {
                        "username": "redfish-user",
                        "password": "redfish-pass",
                    }
                }
            }
        elif request.url.path == (
            "/v1/pki/data/tenants/tenant-id/projects/project-id/redfish/ca"
        ):
            payload = {
                "data": {
                    "data": {
                        "pem": (
                            "-----BEGIN CERTIFICATE-----\\n"
                            "CA\\n"
                            "-----END CERTIFICATE-----"
                        )
                    }
                }
            }
        else:
            return httpx.Response(404, json={"errors": ["not found"]})
        return httpx.Response(200, json=payload)

    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    settings.BMC_VAULT_NAMESPACE = "tenant/platform"
    settings.BMC_VAULT_CA_BUNDLE = ""
    monkeypatch.setattr(
        vault_module.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    result = resolve_bmc_credentials(
        "vault://secret/bmc/rack-a",
        "vault://pki/redfish/ca",
        tenant_id="tenant-id",
        project_id="project-id",
    )
    assert result["username"] == "redfish-user"
    assert result["password"] == "redfish-pass"
    assert "BEGIN CERTIFICATE" in result["trust_bundle_pem"]
    assert len(requests) == 2
    assert all(
        request.headers["X-Vault-Token"] == "vault-test-token"
        for request in requests
    )
    assert all(
        request.headers["X-Vault-Namespace"] == "tenant/platform"
        for request in requests
    )


def test_vault_resolver_rejects_missing_secret_fields(settings, monkeypatch):
    import core.bmc_credentials as vault_module
    import httpx

    original_client = httpx.Client

    def handler(request):
        return httpx.Response(
            200,
            json={"data": {"data": {"username": "only-user"}}},
        )

    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    settings.BMC_VAULT_NAMESPACE = ""
    settings.BMC_VAULT_CA_BUNDLE = ""
    monkeypatch.setattr(
        vault_module.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    with pytest.raises(ValueError, match="username and password"):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id="tenant-id",
            project_id="project-id",
        )


@pytest.mark.parametrize("status_code", [302, 403, 404, 500])
def test_vault_resolver_rejects_http_errors_without_leaking_response(
    settings, monkeypatch, status_code
):
    import core.bmc_credentials as vault_module

    original_client = httpx.Client

    def handler(request):
        return httpx.Response(
            status_code,
            headers={"location": "https://attacker.example/steal"},
            text="sensitive upstream response",
        )

    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(
        vault_module.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    with pytest.raises(RuntimeError, match="lookup failed"):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id="tenant-id",
            project_id="project-id",
        )


@pytest.mark.parametrize(
    "error",
    [
        __import__("httpx").ReadTimeout("read timeout"),
        __import__("httpx").ConnectError("connection failed"),
        __import__("httpx").ConnectError("TLS handshake failed"),
    ],
)
def test_vault_resolver_propagates_transport_errors_without_secret_values(
    settings, monkeypatch, error
):
    import core.bmc_credentials as vault_module

    original_client = httpx.Client

    def handler(request):
        raise error

    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(
        vault_module.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    with pytest.raises(type(error)):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id="tenant-id",
            project_id="project-id",
        )


def test_vault_resolver_rejects_non_json_response(settings, monkeypatch):
    import core.bmc_credentials as vault_module

    original_client = httpx.Client

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html>upstream error</html>",
        )

    settings.BMC_VAULT_TOKEN = "vault-test-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(
        vault_module.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    with pytest.raises(ValueError, match="non-JSON"):
        resolve_bmc_credentials(
            "vault://secret/bmc/rack-a",
            tenant_id="tenant-id",
            project_id="project-id",
        )

 
def test_vault_scoped_token_resolver_receives_tenant_and_project(settings, monkeypatch):
    import core.bmc_credentials as vault_module

    calls = []
    original_client = httpx.Client

    def token_resolver(*, tenant_id, project_id):
        calls.append((tenant_id, project_id))
        return "scoped-vault-token"

    def handler(request):
        return httpx.Response(200, json={"data": {"data": {"username": "redfish-user", "password": "redfish-pass"}}})

    settings.BMC_VAULT_TOKEN = "global-fallback-token"
    settings.BMC_VAULT_TOKEN_RESOLVER = "tests.token_resolver"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(vault_module, "import_string", lambda path: token_resolver)
    monkeypatch.setattr(vault_module.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    result = resolve_bmc_credentials("vault://secret/bmc/rack-a", tenant_id="tenant-42", project_id="project-9")
    assert result["username"] == "redfish-user"
    assert calls == [("tenant-42", "project-9")]


@pytest.mark.parametrize("invalid_token", ["", " token", "token ", "two words", None, 123])
def test_vault_scoped_token_resolver_rejects_invalid_token_before_network(settings, monkeypatch, invalid_token):
    import core.bmc_credentials as vault_module

    def token_resolver(*, tenant_id, project_id):
        return invalid_token

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted with an invalid token.")

    settings.BMC_VAULT_TOKEN = "global-fallback-token"
    settings.BMC_VAULT_TOKEN_RESOLVER = "tests.token_resolver"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(vault_module, "import_string", lambda path: token_resolver)
    monkeypatch.setattr(vault_module.httpx, "Client", unexpected_client)
    with pytest.raises(RuntimeError, match="invalid token"):
        resolve_bmc_credentials("vault://secret/bmc/rack-a", tenant_id="tenant-id", project_id="project-id")


def test_invalid_scope_does_not_call_scoped_token_resolver(settings, monkeypatch):
    import core.bmc_credentials as vault_module

    calls = []

    def token_resolver(*, tenant_id, project_id):
        calls.append((tenant_id, project_id))
        return "scoped-token"

    settings.BMC_VAULT_TOKEN_RESOLVER = "tests.token_resolver"
    settings.BMC_VAULT_TOKEN = "global-fallback-token"
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(vault_module, "import_string", lambda path: token_resolver)
    with pytest.raises(ValueError, match="scope"):
        resolve_bmc_credentials("vault://secret/bmc/rack-a", tenant_id="../tenant", project_id="project-id")
    assert calls == []


def test_vault_static_token_is_fallback_when_scoped_resolver_unset(settings, monkeypatch):
    import core.bmc_credentials as vault_module

    original_client = httpx.Client
    observed_tokens = []

    def handler(request):
        observed_tokens.append(request.headers["X-Vault-Token"])
        return httpx.Response(200, json={"data": {"data": {"username": "redfish-user", "password": "redfish-pass"}}})

    settings.BMC_VAULT_TOKEN = "single-scope-static-token"
    settings.BMC_VAULT_TOKEN_RESOLVER = ""
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    monkeypatch.setattr(vault_module.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    resolve_bmc_credentials("vault://secret/bmc/rack-a", tenant_id="tenant-id", project_id="project-id")
    assert observed_tokens == ["single-scope-static-token"]
