"""The payment credential runbook must stay true to the code it describes.

`docs/payment-credentials.md` is the operator sequence for connecting a real
merchant account. A runbook that drifts from the adapters is worse than none:
it names variables that no longer exist and hides slots that now block
checkout. These tests read the adapters and the doc together, so adding a slot,
renaming a reference or changing the allowlist fails the suite until the runbook
is updated in the same commit.
"""

import re
from pathlib import Path

import pytest
from core.payments.alipay import AlipayProvider
from core.payments.base import payment_env_prefixes
from core.payments.wechat_v3 import WechatPayV3Provider

DOC = Path(__file__).resolve().parents[1] / "docs" / "payment-credentials.md"
ENV_REFERENCE = re.compile(r"env://([A-Z0-9_]+)")


@pytest.fixture(scope="module")
def runbook() -> str:
    assert DOC.is_file(), "the payment credential runbook is missing"
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("provider_class", [WechatPayV3Provider, AlipayProvider])
def test_every_declared_slot_is_documented(runbook, provider_class):
    for slot in provider_class.SLOTS:
        assert f"`{slot}`" in runbook, f"slot {slot} of {provider_class.channel} is undocumented"


@pytest.mark.parametrize("provider_class", [WechatPayV3Provider, AlipayProvider])
def test_every_default_reference_is_documented(runbook, provider_class):
    names = set(ENV_REFERENCE.findall(repr(provider_class.__init__.__defaults__)))
    assert names, f"{provider_class.channel} declares no env reference defaults"
    for name in sorted(names):
        assert name in runbook, f"default reference env://{name} is undocumented"


def test_required_slots_are_documented_as_required(runbook):
    table_rows = {}
    for line in runbook.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[2] in {"yes", "no"}:
            table_rows[cells[0].strip("`")] = cells[2]
    for provider_class in (WechatPayV3Provider, AlipayProvider):
        for slot in provider_class.SLOTS:
            required = "yes" if slot in provider_class.REQUIRED_SLOTS else "no"
            assert table_rows.get(slot) == required, (
                f"{provider_class.channel} slot {slot} documented as "
                f"{table_rows.get(slot)!r}, code says {required!r}"
            )


def test_effective_prefixes_and_self_check_are_documented(runbook):
    for prefix in payment_env_prefixes():
        assert prefix in runbook, f"environment prefix {prefix} is undocumented"
    assert "PAYMENT_CREDENTIAL_ENV_PREFIXES" in runbook
    assert "check_payment_channels" in runbook


def test_vault_reference_format_is_documented(runbook):
    assert "vault://" in runbook
    assert "mount/path#field" in runbook


def test_runbook_documents_the_gates_it_claims(runbook):
    for token in (
        "PAYMENT_PROVIDER_NOT_CONFIGURED",
        "PAYMENT_SIGNATURE_INVALID",
        "PAYMENT_AMOUNT_MISMATCH",
        "PURCHASE_REGION_NOT_SUPPORTED",
        "PAYMENT_CHANNEL_NOT_SUPPORTED",
        "BILLING_FAKE_PROVIDER_ENABLED",
    ):
        assert token in runbook


def test_runbook_contains_no_secret_material_or_placeholder(runbook):
    for forbidden in ("-----BEGIN", "BEGIN RSA PRIVATE KEY", "sk-live-", "TODO", "TBD", "<your"):
        assert forbidden not in runbook, f"runbook contains forbidden token {forbidden!r}"
