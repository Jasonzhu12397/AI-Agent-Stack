"""Provider registry: build the configured provider set at call time.

Providers whose credential references do not resolve are excluded, so a
deployment with no configured channel reports an empty provider set. The fake
test double is added only behind the explicit settings override.
"""

from django.conf import settings

from core.payments.alipay import AlipayProvider
from core.payments.base import PaymentProvider
from core.payments.fake import FakeProvider
from core.payments.wechat_v3 import WechatPayV3Provider

_PROVIDER_FACTORIES = (WechatPayV3Provider, AlipayProvider)


def build_providers() -> dict[str, PaymentProvider]:
    providers: dict[str, PaymentProvider] = {}
    for factory in _PROVIDER_FACTORIES:
        provider = factory()
        if provider.is_configured():
            providers[provider.channel] = provider
    if getattr(settings, "BILLING_FAKE_PROVIDER_ENABLED", False):
        fake = FakeProvider()
        providers[fake.channel] = fake
    return providers


def get_provider(channel: str) -> PaymentProvider | None:
    return build_providers().get(channel)


def available_channels() -> list[str]:
    return sorted(build_providers().keys())


def has_configured_provider() -> bool:
    return bool(build_providers())
