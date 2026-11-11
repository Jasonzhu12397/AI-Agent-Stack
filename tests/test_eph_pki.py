# ruff: noqa: I001
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from core.eph_pki import (
    EPHCertificateError,
    _validate_ca_chain,
    validate_eph_csr,
    _validate_issued_client_certificate,
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
        .public_bytes(serialization.Encoding.PEM)
        .decode("ascii")
    )


def test_validate_eph_csr_accepts_valid_signature_and_normalizes_final_newline():
    csr = _csr()

    assert validate_eph_csr(csr, "agent-node-test.agents.internal").endswith("\n")


def test_validate_eph_csr_rejects_wrong_node_identity():
    with pytest.raises(EPHCertificateError, match="SUBJECT_INVALID"):
        validate_eph_csr(
            _csr("agent-node-other.agents.internal"),
            "agent-node-test.agents.internal",
        )


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



def _issued_certificate(
    not_before=None,
    not_after=None,
    include_client_auth=True,
    ca=False,
):
    from datetime import datetime, timedelta

    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "agent-node-test.agents.internal"),
    ])
    now = datetime.utcnow()
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(minutes=1))
        .not_valid_after(not_after or now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    usages = [ExtendedKeyUsageOID.CLIENT_AUTH] if include_client_auth else [
        ExtendedKeyUsageOID.SERVER_AUTH
    ]
    certificate = (
        builder.add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    return certificate


def test_issued_client_certificate_requires_client_auth_profile():
    certificate = _issued_certificate()

    _validate_issued_client_certificate(
        certificate,
        "agent-node-test.agents.internal",
    )


@pytest.mark.parametrize(
    "certificate",
    [
        _issued_certificate(include_client_auth=False),
        _issued_certificate(ca=True),
    ],
)
def test_issued_client_certificate_rejects_wrong_usage_or_ca(certificate):
    with pytest.raises(EPHCertificateError, match="PROFILE_INVALID"):
        _validate_issued_client_certificate(
            certificate,
            "agent-node-test.agents.internal",
        )


def test_issued_client_certificate_rejects_expired_certificate():
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    certificate = _issued_certificate(
        not_before=now - timedelta(hours=2),
        not_after=now - timedelta(hours=1),
    )

    with pytest.raises(EPHCertificateError, match="VALIDITY_INVALID"):
        _validate_issued_client_certificate(
            certificate,
            "agent-node-test.agents.internal",
        )


def test_issued_client_certificate_rejects_wrong_node_identity():
    certificate = _issued_certificate()

    with pytest.raises(EPHCertificateError, match="PROFILE_INVALID"):
        _validate_issued_client_certificate(
            certificate,
            "agent-node-other.agents.internal",
        )
