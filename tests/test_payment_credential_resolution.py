"""Payment credentials must resolve through the platform's single resolver.

The payment adapters and the plugin connector stack both reference secrets by
URI (``env://NAME``, ``vault://mount/path#field``). A second, payment-only
resolver is a bug surface: it silently fails for ``vault://`` references, so a
deployment that stores merchant keys in Vault reports "no payment provider"
forever, and its ``env://`` lookups bypass the allowlist that protects every
other credential in the platform.

Covered invariants:

* a ``vault://`` merchant credential resolves to the same plaintext the plugin
  credential resolver returns for the same reference;
* environment lookups for payment channels are confined to an explicit prefix
  allowlist, so an unrelated process variable cannot become a merchant key;
* an unresolvable reference keeps the channel out of the registry instead of
  raising (the capability endpoint keeps reporting "not configured");
* a reference without a known scheme, or a bare secret value, is rejected;
* no module under ``core/payments`` reads the process environment directly, so
  the resolver stays authoritative.
"""

import re
from pathlib import Path

import core.plugin_credentials as plugin_credentials
import httpx
import pytest
from core.payments.base import resolve_credential_ref
from core.payments.registry import build_providers, get_provider
from core.payments.wechat_v3 import WechatPayV3Provider

RealHttpxClient = httpx.Client
PAYMENTS_DIR = Path(__file__).resolve().parents[1] / "apps" / "platform-api" / "core" / "payments"


@pytest.fixture
def clean_secret_env(monkeypatch):
    for name in (
        "PLUGIN_VAULT_ADDR",
        "PLUGIN_VAULT_TOKEN",
        "PLUGIN_VAULT_TOKEN_RESOLVER",
        "PLUGIN_VAULT_TIMEOUT_SECONDS",
        "PLUGIN_VAULT_ALLOW_INSECURE",
        "WECHATPAY_APPID",
        "WECHATPAY_API_V3_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _mock_vault(settings, monkeypatch, secret: dict):
    captured: dict = {}

    def factory(**options):
        options.pop("transport", None)
        captured["options"] = options

        def handler(request):
            captured["path"] = request.url.path
            captured["token"] = request.headers.get("X-Vault-Token")
            return httpx.Response(200, json={"data": {"data": secret}})

        return RealHttpxClient(transport=httpx.MockTransport(handler), **options)

    settings.PLUGIN_VAULT_ADDR = "https://vault.example.net"
    settings.PLUGIN_VAULT_TOKEN = "vault-token-secret"
    monkeypatch.setattr(plugin_credentials.httpx, "Client", factory)
    return captured


def test_payment_vault_reference_matches_plugin_credential_resolution(
    settings, clean_secret_env, monkeypatch
):
    secret = {"api_v3_key": "vault-api-v3-key-9"}
    captured = _mock_vault(settings, monkeypatch, secret)
    reference = "vault://merchant/wechatpay#api_v3_key"

    provider = WechatPayV3Provider(api_v3_key_ref=reference)
    payment_value = provider._resolve("api_v3_key")
    plugin_contract = plugin_credentials.resolve(reference)

    assert payment_value == "vault-api-v3-key-9"
    assert plugin_contract == {"type": "bearer", "token": "vault-api-v3-key-9"}
    assert captured["path"] == "/v1/merchant/data/wechatpay"
    assert captured["token"] == "vault-token-secret"


def test_payment_env_lookup_is_confined_to_channel_prefix(
    settings, clean_secret_env, monkeypatch
):
    monkeypatch.setenv("SECRET_KEY", "unrelated-process-secret")
    monkeypatch.setenv("PLUGIN_SECRET_PAYMENT", "plugin-owned-secret")

    assert resolve_credential_ref("env://SECRET_KEY") is None
    assert resolve_credential_ref("env://PLUGIN_SECRET_PAYMENT") is None

    monkeypatch.setenv("WECHATPAY_APPID", "wx-appid-test")
    assert resolve_credential_ref("env://WECHATPAY_APPID") == "wx-appid-test"


def test_unresolved_vault_reference_keeps_channel_out_of_registry(
    settings, clean_secret_env, monkeypatch
):
    reference = "vault://merchant/wechatpay#api_v3_key"
    provider = WechatPayV3Provider(
        appid_ref=reference,
        mchid_ref=reference,
        api_v3_key_ref=reference,
        serial_no_ref=reference,
        private_key_ref=reference,
        platform_cert_ref=reference,
    )

    assert provider.is_configured() is False
    assert build_providers() == {}
    assert get_provider("wechat") is None


def test_reference_without_scheme_or_bare_secret_is_rejected(
    settings, clean_secret_env, monkeypatch
):
    monkeypatch.setenv("WECHATPAY_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    bare = "-----BEGIN PRIVATE KEY-----"

    assert resolve_credential_ref(bare) is None
    assert resolve_credential_ref("WECHATPAY_PRIVATE_KEY") is None
    with pytest.raises(plugin_credentials.CredentialError):
        plugin_credentials.resolve(bare)


def test_no_payment_module_reads_process_environment_directly():
    offenders = []
    for path in PAYMENTS_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if re.search(r"os\.environ|os\.getenv|from os import environ", source):
            offenders.append(path.name)

    assert offenders == []
