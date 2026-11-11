# ruff: noqa: I001
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from core.eph_pki import (
    EPHCertificateError,
    _validate_ca_chain,
    validate_eph_csr,
)


def _csr(common_name="agent-node-test.agents.internal"):
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .sign(private_key, hashes.SHA256())
        .public_bytes(__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.PEM)
        .decode("ascii")
    )


def test_validate_eph_csr_accepts_valid_signature_and_normalizes_final_newline():
    csr = _csr()

    assert validate_eph_csr(csr, "agent-node-test.agents.internal").endswith("\n")


def test_validate_eph_csr_rejects_wrong_node_identity():
    with pytest.raises(EPHCertificateError, match="SUBJECT_INVALID"):
        validate_eph_csr(_csr("agent-node-other.agents.internal"), "agent-node-test.agents.internal")


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
        "-----BEGIN CERTIFICATE REQUEST-----\nMAA=\n-----END CERTIFICATE REQUEST-----",
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
