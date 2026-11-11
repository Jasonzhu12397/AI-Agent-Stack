# ruff: noqa: I001
import pytest

from core.eph_pki import (
    EPHCertificateError,
    _validate_ca_chain,
    validate_eph_csr,
)


def test_validate_eph_csr_accepts_pem_and_normalizes_final_newline():
    csr = "-----BEGIN CERTIFICATE REQUEST-----\nMAA=\n-----END CERTIFICATE REQUEST-----"

    assert validate_eph_csr(csr).endswith("\n")


@pytest.mark.parametrize(
    "csr",
    [
        "",
        "not-a-csr",
        "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----",
        "-----BEGIN CERTIFICATE REQUEST-----\n***\n-----END CERTIFICATE REQUEST-----",
        (
            "-----BEGIN CERTIFICATE REQUEST-----\nMAA=\n"
            "-----END CERTIFICATE REQUEST-----"
            + ("A" * 16384)
        ),
    ],
)
def test_validate_eph_csr_rejects_invalid_or_oversized_pem(csr):
    with pytest.raises(EPHCertificateError):
        validate_eph_csr(csr)


def test_vault_ca_chain_is_ordered_from_issuing_ca_and_deduplicated():
    issuing_ca = "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----"
    root_ca = "-----BEGIN CERTIFICATE-----\nMAE=\n-----END CERTIFICATE-----"

    chain = _validate_ca_chain([issuing_ca, root_ca], issuing_ca)

    assert chain == issuing_ca + "\n" + root_ca + "\n"


def test_vault_ca_chain_rejects_missing_or_invalid_chain():
    with pytest.raises(EPHCertificateError):
        _validate_ca_chain([], "")

    with pytest.raises(EPHCertificateError):
        _validate_ca_chain(["not-a-certificate"], "")
