# ruff: noqa: I001
import httpx
import pytest

import core.plugin_credentials as plugin_credentials


RealHttpxClient = httpx.Client


@pytest.fixture
def clean_vault_env(monkeypatch):
    for name in (
        "PLUGIN_VAULT_ADDR",
        "PLUGIN_VAULT_TOKEN",
        "PLUGIN_VAULT_TOKEN_RESOLVER",
        "PLUGIN_VAULT_TIMEOUT_SECONDS",
        "PLUGIN_VAULT_ALLOW_INSECURE",
    ):
        monkeypatch.delenv(name, raising=False)


def _mock_client_factory(handler, captured):
    def factory(**options):
        options.pop("transport", None)
        captured["options"] = options
        return RealHttpxClient(transport=httpx.MockTransport(handler), **options)

    return factory


def _kv_v2_response(secret: dict) -> dict:
    return {"data": {"data": secret}}


def test_vault_provider_is_registered():
    assert "vault" in plugin_credentials.providers()


def test_vault_resolves_plain_string_as_bearer(settings, clean_vault_env, monkeypatch, caplog):
    captured: dict = {}
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_kv_v2_response({"value": "vault-bearer-42"}))

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(
        plugin_credentials.httpx,
        "Client",
        _mock_client_factory(handler, captured),
    )

    credential = plugin_credentials.resolve("vault://secret/research-agent")

    assert credential == {"type": "bearer", "token": "vault-bearer-42"}
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/secret/data/research-agent"
    assert requests[0].headers["X-Vault-Token"] == "vault-token-secret"
    assert captured["options"]["follow_redirects"] is False
    assert "vault-token-secret" not in caplog.text
    assert "vault-bearer-42" not in caplog.text


def test_vault_resolves_header_contract_from_named_field(settings, clean_vault_env, monkeypatch):
    captured: dict = {}

    def handler(request):
        return httpx.Response(
            200,
            json=_kv_v2_response(
                {
                    "api_key": {
                        "type": "header",
                        "name": "X-Api-Key",
                        "value": "header-secret-17",
                    }
                }
            ),
        )

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(
        plugin_credentials.httpx,
        "Client",
        _mock_client_factory(handler, captured),
    )

    credential = plugin_credentials.resolve("vault://secret/research-agent#api_key")

    assert credential == {
        "type": "header",
        "name": "X-Api-Key",
        "value": "header-secret-17",
    }


def test_vault_resolves_query_contract(settings, clean_vault_env, monkeypatch):
    captured: dict = {}

    def handler(request):
        return httpx.Response(
            200,
            json=_kv_v2_response(
                {
                    "value": {
                        "type": "query",
                        "name": "key",
                        "value": "query-secret-31",
                    }
                }
            ),
        )

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(
        plugin_credentials.httpx,
        "Client",
        _mock_client_factory(handler, captured),
    )

    credential = plugin_credentials.resolve("vault://secret/research-agent")

    assert credential == {
        "type": "query",
        "name": "key",
        "value": "query-secret-31",
    }


def test_vault_404_maps_to_not_found(settings, clean_vault_env, monkeypatch, caplog):
    def handler(request):
        return httpx.Response(404, json={"errors": ["missing"]})

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", _mock_client_factory(handler, {}))

    with pytest.raises(plugin_credentials.CredentialNotFoundError) as exc:
        plugin_credentials.resolve("vault://secret/missing-agent")

    assert exc.value.code == "CREDENTIAL_NOT_FOUND"
    assert "vault-token-secret" not in str(exc.value)
    assert "vault-token-secret" not in caplog.text


def test_vault_403_maps_to_access_denied(settings, clean_vault_env, monkeypatch, caplog):
    def handler(request):
        return httpx.Response(403, json={"errors": ["denied"]})

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", _mock_client_factory(handler, {}))

    with pytest.raises(plugin_credentials.CredentialAccessDeniedError) as exc:
        plugin_credentials.resolve("vault://secret/denied-agent")

    assert exc.value.code == "CREDENTIAL_ACCESS_DENIED"
    assert "vault-token-secret" not in str(exc.value)
    assert "vault-token-secret" not in caplog.text


@pytest.mark.parametrize(
    "reference",
    [
        "vault:///secret/path",
        "vault://secret/../admin",
        "vault://secret/./admin",
        "vault://secret/path?version=2",
        "vault://user:pass@secret/path",
        "vault://secret/path#bad field",
        "vault://se cret/path",
    ],
)
def test_vault_rejects_invalid_reference_before_network(
    settings, clean_vault_env, monkeypatch, reference
):
    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted for an invalid reference.")

    monkeypatch.setattr(plugin_credentials.httpx, "Client", unexpected_client)

    with pytest.raises(plugin_credentials.CredentialReferenceInvalidError) as exc:
        plugin_credentials.resolve(reference)

    assert exc.value.code == "CREDENTIAL_REFERENCE_INVALID"


def test_vault_not_configured_without_address(settings, clean_vault_env, monkeypatch):
    settings.PLUGIN_VAULT_ADDR = ""
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted without an address.")

    monkeypatch.setattr(plugin_credentials.httpx, "Client", unexpected_client)

    with pytest.raises(plugin_credentials.CredentialResolverNotConfiguredError) as exc:
        plugin_credentials.resolve("vault://secret/research-agent")

    assert exc.value.code == "CREDENTIAL_RESOLVER_NOT_CONFIGURED"


def test_vault_not_configured_without_token(settings, clean_vault_env, monkeypatch):
    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = ""

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted without a token.")

    monkeypatch.setattr(plugin_credentials.httpx, "Client", unexpected_client)

    with pytest.raises(plugin_credentials.CredentialResolverNotConfiguredError) as exc:
        plugin_credentials.resolve("vault://secret/research-agent")

    assert exc.value.code == "CREDENTIAL_RESOLVER_NOT_CONFIGURED"


def test_vault_token_resolver_dotted_path_supplies_token(settings, clean_vault_env, monkeypatch):
    calls: list[str] = []

    def token_resolver():
        calls.append("resolved")
        return "resolver-token-99"

    captured: dict = {}

    def handler(request):
        assert request.headers["X-Vault-Token"] == "resolver-token-99"
        return httpx.Response(200, json=_kv_v2_response({"value": "bearer-from-resolver"}))

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "global-fallback-token"
    settings.PLUGIN_VAULT_TOKEN_RESOLVER = "tests.token_resolver"
    monkeypatch.setattr(plugin_credentials, "import_string", lambda path: token_resolver)
    monkeypatch.setattr(
        plugin_credentials.httpx,
        "Client",
        _mock_client_factory(handler, captured),
    )

    credential = plugin_credentials.resolve("vault://secret/research-agent")

    assert calls == ["resolved"]
    assert credential == {"type": "bearer", "token": "bearer-from-resolver"}


def test_vault_resolver_invalid_token_fails_closed(settings, clean_vault_env, monkeypatch):
    def token_resolver():
        return "  "

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "global-fallback-token"
    settings.PLUGIN_VAULT_TOKEN_RESOLVER = "tests.token_resolver"
    monkeypatch.setattr(plugin_credentials, "import_string", lambda path: token_resolver)

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted with an invalid token.")

    monkeypatch.setattr(plugin_credentials.httpx, "Client", unexpected_client)

    with pytest.raises(plugin_credentials.CredentialResolverNotConfiguredError):
        plugin_credentials.resolve("vault://secret/research-agent")


def test_vault_rejects_insecure_http_by_default(settings, clean_vault_env, monkeypatch):
    settings.PLUGIN_VAULT_ADDR = "http://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not be contacted over plain HTTP.")

    monkeypatch.setattr(plugin_credentials.httpx, "Client", unexpected_client)

    with pytest.raises(plugin_credentials.CredentialResolverNotConfiguredError) as exc:
        plugin_credentials.resolve("vault://secret/research-agent")

    assert exc.value.code == "CREDENTIAL_RESOLVER_NOT_CONFIGURED"


def test_vault_localhost_http_requires_explicit_insecure_flag(
    settings, clean_vault_env, monkeypatch
):
    settings.PLUGIN_VAULT_ADDR = "http://127.0.0.1:8200"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    settings.PLUGIN_VAULT_ALLOW_INSECURE = False

    def unexpected_client(**kwargs):
        raise AssertionError("Vault must not use HTTP without the insecure flag.")

    monkeypatch.setattr(plugin_credentials.httpx, "Client", unexpected_client)

    with pytest.raises(plugin_credentials.CredentialResolverNotConfiguredError):
        plugin_credentials.resolve("vault://secret/research-agent")


def test_vault_localhost_http_allowed_with_explicit_flag(settings, clean_vault_env, monkeypatch):
    captured: dict = {}

    def handler(request):
        return httpx.Response(200, json=_kv_v2_response({"value": "local-bearer"}))

    settings.PLUGIN_VAULT_ADDR = "http://127.0.0.1:8200"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    settings.PLUGIN_VAULT_ALLOW_INSECURE = True
    monkeypatch.setattr(
        plugin_credentials.httpx,
        "Client",
        _mock_client_factory(handler, captured),
    )

    credential = plugin_credentials.resolve("vault://secret/research-agent")

    assert credential == {"type": "bearer", "token": "local-bearer"}


def test_vault_rejects_non_object_payload(settings, clean_vault_env, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": {"data": ["not", "an", "object"]}})

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", _mock_client_factory(handler, {}))

    with pytest.raises(plugin_credentials.CredentialInvalidShapeError) as exc:
        plugin_credentials.resolve("vault://secret/research-agent")

    assert exc.value.code == "CREDENTIAL_INVALID_SHAPE"


def test_vault_rejects_missing_field(settings, clean_vault_env, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_kv_v2_response({"other": "x"}))

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", _mock_client_factory(handler, {}))

    with pytest.raises(plugin_credentials.CredentialInvalidShapeError) as exc:
        plugin_credentials.resolve("vault://secret/research-agent#api_key")

    assert exc.value.code == "CREDENTIAL_INVALID_SHAPE"


def test_vault_rejects_non_contract_object(settings, clean_vault_env, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_kv_v2_response({"value": {"type": "cookie"}}))

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", _mock_client_factory(handler, {}))

    with pytest.raises(plugin_credentials.CredentialInvalidShapeError):
        plugin_credentials.resolve("vault://secret/research-agent")


def test_vault_rejects_non_json_body(settings, clean_vault_env, monkeypatch):
    def handler(request):
        return httpx.Response(200, content="<html>oops</html>")

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", _mock_client_factory(handler, {}))

    with pytest.raises(plugin_credentials.CredentialInvalidShapeError):
        plugin_credentials.resolve("vault://secret/research-agent")
