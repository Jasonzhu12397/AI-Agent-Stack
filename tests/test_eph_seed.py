# ruff: noqa: I001
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from core.eph_seed import EPHSeedError, render_nocloud_seed


def make_ca_certificate(common_name, expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2 if expired else 1))
        .not_valid_after(
            now - timedelta(days=1) if expired else now + timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def valid_config():
    return {
        "control_url": "https://agent-control.example.com",
        "node_id": str(uuid.uuid4()),
        "enrollment_token": "a" * 64,
        "agent_image": "registry.example.com/ai-agent@sha256:" + "a" * 64,
        "hostname": "eph-node-01",
        "network_config": {
            "version": 2,
            "ethernets": {
                "uplink": {
                    "match": {"name": "en*"},
                    "dhcp4": True,
                    "dhcp6": False,
                }
            },
        },
        "ca_certificate": make_ca_certificate("control-ca"),
        "agent_ca_certificate": make_ca_certificate("agent-ca"),
    }


def test_render_nocloud_seed_creates_all_three_datasource_files():
    seed = render_nocloud_seed(valid_config())

    assert set(seed) == {"user-data", "meta-data", "network-config"}
    assert seed["user-data"].startswith("#cloud-config\n")
    user_data = json.loads(seed["user-data"].split("\n", 1)[1])
    metadata = json.loads(seed["meta-data"])
    network = json.loads(seed["network-config"])
    assert metadata["instance-id"].startswith("eph-node-")
    assert metadata["local-hostname"] == "eph-node-01"
    assert network["version"] == 2
    assert len(user_data["write_files"]) == 3
    assert not any("tls.key" in item["path"] for item in user_data["write_files"])
    assert not any("tls.crt" in item["path"] for item in user_data["write_files"])
    agent_ca = next(
        item
        for item in user_data["write_files"]
        if item["path"].endswith("/tls/agent-ca.crt")
    )
    assert "BEGIN CERTIFICATE" in agent_ca["content"]


def test_seed_secrets_are_written_with_root_only_permissions():
    seed = render_nocloud_seed(valid_config())
    user_data = json.loads(seed["user-data"].split("\n", 1)[1])

    for item in user_data["write_files"]:
        assert item["owner"] == "root:root"
        assert item["permissions"] == "0600"

    assert "enrollment_token" in user_data["write_files"][0]["content"]
    assert "a" * 64 in user_data["write_files"][0]["content"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"control_url": "http://control.example.com"}, "EPH_SEED_CONTROL_URL_INVALID"),
        ({"control_url": "https://control.example.com/path"}, "EPH_SEED_CONTROL_URL_INVALID"),
        ({"control_url": "https://"}, "EPH_SEED_CONTROL_URL_INVALID"),
        ({"node_id": "invalid"}, "EPH_SEED_NODE_ID_INVALID"),
        ({"enrollment_token": "short"}, "EPH_SEED_ENROLLMENT_TOKEN_INVALID"),
        (
            {"agent_image": "registry.example.com/agent:latest"},
            "EPH_SEED_AGENT_IMAGE_NOT_PINNED",
        ),
        ({"hostname": "../invalid"}, "EPH_SEED_HOSTNAME_INVALID"),
        ({"network_config": {"version": 1}}, "EPH_SEED_NETWORK_CONFIG_INVALID"),
        (
            {"ca_certificate": "not-a-certificate"},
            "EPH_SEED_CREDENTIAL_INVALID:ca_certificate",
        ),
        (
            {"agent_ca_certificate": "not-a-certificate"},
            "EPH_SEED_CREDENTIAL_INVALID:agent_ca_certificate",
        ),
    ],
)
def test_seed_rejects_invalid_node_configuration(change, expected):
    config = valid_config()
    config.update(change)

    with pytest.raises(EPHSeedError, match=expected):
        render_nocloud_seed(config)


def test_seed_requires_independent_agent_issuer_ca():
    config = valid_config()
    del config["agent_ca_certificate"]

    with pytest.raises(
        EPHSeedError,
        match="EPH_SEED_CREDENTIAL_INVALID:agent_ca_certificate",
    ):
        render_nocloud_seed(config)


def test_seed_rejects_same_ca_with_different_pem_wrapping():
    config = valid_config()
    certificate = config["ca_certificate"]
    encoded = "".join(
        line for line in certificate.splitlines()
        if not line.startswith("-----")
    )
    der = base64.b64decode(encoded)
    wrapped = base64.b64encode(der).decode("ascii")
    config["agent_ca_certificate"] = (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(
            wrapped[index:index + 48] for index in range(0, len(wrapped), 48)
        )
        + "\n-----END CERTIFICATE-----\n"
    )

    with pytest.raises(
        EPHSeedError,
        match="EPH_SEED_AGENT_CA_MUST_BE_INDEPENDENT",
    ):
        render_nocloud_seed(config)


def test_seed_rejects_non_ca_certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    config = valid_config()
    config["agent_ca_certificate"] = certificate.public_bytes(
        serialization.Encoding.PEM
    ).decode("ascii")

    with pytest.raises(
        EPHSeedError,
        match="EPH_SEED_CA_CERTIFICATE_INVALID:agent_ca_certificate",
    ):
        render_nocloud_seed(config)


def test_seed_does_not_accept_missing_network_configuration():
    config = valid_config()
    del config["network_config"]

    with pytest.raises(EPHSeedError, match="EPH_SEED_NETWORK_CONFIG_INVALID"):
        render_nocloud_seed(config)


def test_seed_rejects_expired_ca_certificate():
    config = valid_config()
    config["agent_ca_certificate"] = make_ca_certificate("expired-agent-ca", expired=True)

    with pytest.raises(
        EPHSeedError,
        match="EPH_SEED_CA_CERTIFICATE_INVALID:agent_ca_certificate",
    ):
        render_nocloud_seed(config)
