import base64
import datetime
import hashlib
import json
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from node_agent import certificate_rotation


def _create_ca():
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "EPH Test Root"),
    ])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _issue_client_certificate(ca_key, ca_certificate, node_id, validity_hours=24):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(
            NameOID.COMMON_NAME,
            f"agent-node-{node_id}.agents.internal",
        ),
    ])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=validity_hours))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    ca_pem = ca_certificate.public_bytes(serialization.Encoding.PEM)
    fingerprint = hashlib.sha256(
        certificate.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    return key, key_pem, certificate, certificate_pem, ca_pem, fingerprint


def _secret_data(certificate_pem, key_pem):
    return {
        "tls.crt": base64.b64encode(certificate_pem).decode("ascii"),
        "tls.key": base64.b64encode(key_pem).decode("ascii"),
    }


def test_validate_issued_certificate_requires_matching_identity_key_and_trusted_chain(tmp_path):
    ca_key, ca_certificate = _create_ca()
    key, key_pem, certificate, certificate_pem, ca_pem, fingerprint = (
        _issue_client_certificate(ca_key, ca_certificate, "node-1")
    )
    trust_path = tmp_path / "ca.pem"
    trust_path.write_bytes(ca_pem)

    actual = certificate_rotation.validate_issued_certificate(
        certificate_pem.decode("ascii"),
        ca_pem.decode("ascii"),
        key,
        "node-1",
        certificate_pem + ca_pem,
    )

    assert actual == fingerprint
    assert key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        "agent-node-node-1.agents.internal"
    )


def test_validate_issued_certificate_rejects_wrong_node_identity():
    ca_key, ca_certificate = _create_ca()
    key, _, _, certificate_pem, ca_pem, _ = _issue_client_certificate(
        ca_key,
        ca_certificate,
        "node-2",
    )

    with pytest.raises(
        certificate_rotation.CertificateRotationError,
        match="AGENT_TLS_ROTATION_CERTIFICATE_IDENTITY_INVALID",
    ):
        certificate_rotation.validate_issued_certificate(
            certificate_pem.decode("ascii"),
            ca_pem.decode("ascii"),
            key,
            "node-1",
            certificate_pem + ca_pem,
        )


def test_validate_issued_certificate_rejects_untrusted_root():
    ca_key, ca_certificate = _create_ca()
    other_key, other_ca = _create_ca()
    key, _, _, certificate_pem, ca_pem, _ = _issue_client_certificate(
        ca_key,
        ca_certificate,
        "node-1",
    )
    other_ca_pem = other_ca.public_bytes(serialization.Encoding.PEM)

    with pytest.raises(
        certificate_rotation.CertificateRotationError,
        match="AGENT_TLS_ROTATION_UNTRUSTED_CA",
    ):
        certificate_rotation.validate_issued_certificate(
            certificate_pem.decode("ascii"),
            ca_pem.decode("ascii"),
            key,
            "node-1",
            other_ca_pem,
        )
    assert other_key is not None


def test_eph_agent_stages_and_activates_certificate_using_node_generated_key(
    monkeypatch,
    tmp_path,
):
    ca_key, ca_certificate = _create_ca()
    old_key, old_key_pem, _, old_cert_pem, ca_pem, _ = _issue_client_certificate(
        ca_key,
        ca_certificate,
        "node-rotation",
        validity_hours=2,
    )
    ca_path = tmp_path / "control-ca.pem"
    cert_path = tmp_path / "active.crt"
    key_path = tmp_path / "active.key"
    ca_path.write_bytes(ca_pem)
    cert_path.write_bytes(old_cert_pem + ca_pem)
    key_path.write_bytes(old_key_pem)
    secret = {"data": _secret_data(old_cert_pem + ca_pem, old_key_pem)}

    monkeypatch.setenv("AGENT_TLS_ROTATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_TLS_CERT_FILE", str(cert_path))
    monkeypatch.setenv("AGENT_TLS_KEY_FILE", str(key_path))
    monkeypatch.setenv("AGENT_TLS_CA_FILE", str(ca_path))
    monkeypatch.setenv("AGENT_TLS_ROTATION_RENEW_BEFORE_HOURS", "6")

    class FakeClient:
        node_id = "node-rotation"
        tls_context = ssl.create_default_context()

        def request(self, path, payload=None, tls_context=None):
            if path.endswith("/certificate/renew/"):
                csr = x509.load_pem_x509_csr(payload["csr_pem"].encode("ascii"))
                now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                certificate = (
                    x509.CertificateBuilder()
                    .subject_name(csr.subject)
                    .issuer_name(ca_certificate.subject)
                    .public_key(csr.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(minutes=1))
                    .not_valid_after(now + datetime.timedelta(hours=24))
                    .add_extension(
                        x509.BasicConstraints(ca=False, path_length=None),
                        critical=True,
                    )
                    .sign(ca_key, hashes.SHA256())
                )
                certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
                fingerprint = hashlib.sha256(
                    certificate.public_bytes(serialization.Encoding.DER)
                ).hexdigest()
                return {
                    "client_certificate": certificate_pem.decode("ascii"),
                    "ca_chain": ca_pem.decode("ascii"),
                    "fingerprint_sha256": fingerprint,
                    "activation_required": True,
                    "private_key_returned": False,
                }
            if path.endswith("/certificate/activate/"):
                assert tls_context is not None
                return {"activated": True, "idempotent": False}
            raise AssertionError(path)

    client = FakeClient()
    manager = certificate_rotation.CertificateRotationManager(client)
    manager._get_secret = lambda: secret

    def patch_secret(data):
        for key, value in data.items():
            if value is None:
                secret["data"].pop(key, None)
            else:
                secret["data"][key] = value
        return secret

    manager._patch_secret = patch_secret

    assert manager.maintain() is True
    assert "pending.tls.crt" not in secret["data"]
    assert "pending.tls.key" not in secret["data"]
    active_certificate = base64.b64decode(secret["data"]["tls.crt"])
    active_key = base64.b64decode(secret["data"]["tls.key"])
    parsed_certificate = x509.load_pem_x509_certificate(active_certificate)
    parsed_key = serialization.load_pem_private_key(active_key, password=None)
    assert parsed_certificate.public_key().public_numbers() == (
        parsed_key.public_key().public_numbers()
    )
    assert parsed_certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        "agent-node-node-rotation.agents.internal"
    )
    assert client.tls_context is not None
