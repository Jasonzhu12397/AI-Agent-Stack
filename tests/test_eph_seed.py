# ruff: noqa: I001
import json
import uuid

import pytest

from core.eph_seed import EPHSeedError, render_nocloud_seed


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
        "ca_certificate": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----",
        "client_certificate": "-----BEGIN CERTIFICATE-----\nCLIENT\n-----END CERTIFICATE-----",
        "client_private_key": "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----",
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
    assert len(user_data["write_files"]) == 4


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
        ({"agent_image": "registry.example.com/agent:latest"}, "EPH_SEED_AGENT_IMAGE_NOT_PINNED"),
        ({"hostname": "../invalid"}, "EPH_SEED_HOSTNAME_INVALID"),
        ({"network_config": {"version": 1}}, "EPH_SEED_NETWORK_CONFIG_INVALID"),
        ({"ca_certificate": "not-a-certificate"}, "EPH_SEED_CREDENTIAL_INVALID:ca_certificate"),
        (\n            {"client_private_key": "-----BEGIN PRIVATE KEY-----"},\n            "EPH_SEED_CREDENTIAL_INVALID:client_private_key",\n        ),
    ],
)
def test_seed_rejects_invalid_node_configuration(change, expected):
    config = valid_config()
    config.update(change)

    with pytest.raises(EPHSeedError, match=expected):
        render_nocloud_seed(config)


def test_seed_does_not_accept_missing_network_or_tls_material():
    config = valid_config()
    del config["network_config"]

    with pytest.raises(EPHSeedError, match="EPH_SEED_NETWORK_CONFIG_INVALID"):
        render_nocloud_seed(config)
