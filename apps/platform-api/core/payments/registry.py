"""Provider registry: build the configured provider set at call time.

Providers whose credential references do not resolve are excluded, so a
deployment with no configured channel reports an empty provider set. The fake
test double is added only behind the explicit settings override.

Readiness is declared by each adapter (``SLOTS``/``REQUIRED_SLOTS``), so this
module only decides what to do with it: expose the configured set for checkout,
or report per-channel slot gaps for operators.
"""

from django.conf import settings

from core.payments.alipay import AlipayProvider
from core.payments.base import PaymentProvider
from core.payments.fake import FakeProvider
from core.payments.wechat_v3 import WechatPayV3Provider

_PROVIDER_FACTORIES = (WechatPayV3Provider, AlipayProvider)


def provider_instances() -> dict[str, PaymentProvider]:
    """Every known channel adapter, configured or not."""

    return {factory.channel: factory() for factory in _PROVIDER_FACTORIES}


def build_providers() -> dict[str, PaymentProvider]:
    providers: dict[str, PaymentProvider] = {}
    for channel, provider in provider_instances().items():
        if provider.is_configured():
            providers[channel] = provider
    if getattr(settings, "BILLING_FAKE_PROVIDER_ENABLED", False):
        fake = FakeProvider()
        providers[fake.channel] = fake
    return providers


def diagnose_channels() -> list[dict]:
    """Readiness per channel, reporting slot names only.

    Slot names are configuration metadata, never a reference URI or a secret
    value, so the report is safe for a tenant-facing diagnostics response.
    """

    entries: list[dict] = []
    for channel, provider in provider_instances().items():
        missing = provider.missing_slots()
        entries.append(
            {
                "channel": channel,
                "configured": not missing,
                "missing": missing,
                "required": list(provider.REQUIRED_SLOTS),
            }
        )
    return entries


def get_provider(channel: str) -> PaymentProvider | None:
    return build_providers().get(channel)


def available_channels() -> list[str]:
    return sorted(build_providers().keys())


def has_configured_provider() -> bool:
    return bool(build_providers())
