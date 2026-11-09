import pytest
from core.bmc_credentials import _parse_vault_reference, resolve_bmc_credentials


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


def test_vault_resolver_fails_closed_without_token(settings):
    settings.BMC_VAULT_TOKEN = ""
    settings.BMC_VAULT_ADDR = "https://vault.example.net"
    with pytest.raises(RuntimeError, match="not configured"):
        resolve_bmc_credentials("vault://secret/bmc/rack-a")


def test_vault_resolver_requires_https(settings):
    settings.BMC_VAULT_TOKEN = "test-token"
    settings.BMC_VAULT_ADDR = "http://vault.example.net"
    with pytest.raises(ValueError, match="HTTPS"):
        resolve_bmc_credentials("vault://secret/bmc/rack-a")


def test_vault_resolver_reads_kv_v2_credentials_and_trust_bundle(
    settings, monkeypatch
):
    import core.bmc_credentials as vault_module
    import httpx

    requests = []
    original_client = httpx.Client

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/secret/data/bmc/rack-a":
            payload = {
                "data": {
                    "data": {
                        "username": "redfish-user",
                        "password": "redfish-pass",
                    }
                }
            }
        elif request.url.path == "/v1/pki/data/redfish/ca":
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
        resolve_bmc_credentials("vault://secret/bmc/rack-a")


@pytest.mark.parametrize("status_code", [302, 403, 404, 500])
def test_vault_resolver_rejects_http_errors_without_leaking_response(
    settings, monkeypatch, status_code
):
    import httpx
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
        resolve_bmc_credentials("vault://secret/bmc/rack-a")


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
    import httpx
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
        resolve_bmc_credentials("vault://secret/bmc/rack-a")


def test_vault_resolver_rejects_non_json_response(settings, monkeypatch):
    import httpx
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
        resolve_bmc_credentials("vault://secret/bmc/rack-a")
