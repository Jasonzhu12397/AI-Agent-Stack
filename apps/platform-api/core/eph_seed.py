# ruff: noqa: I001
import json
import re
import uuid
from urllib.parse import urlsplit


IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$")
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
TOKEN_PATTERN = re.compile(r"^[!-~]{32,512}$")
PEM_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----\s*",
    re.DOTALL,
)


class EPHSeedError(ValueError):
    pass


def _require_string(config, key, minimum=1, maximum=65536):
    value = config.get(key)
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise EPHSeedError("EPH_SEED_FIELD_INVALID:" + key)
    return value


def _validate_network_config(network_config):
    if not isinstance(network_config, dict) or network_config.get("version") != 2:
        raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_INVALID")
    allowed = {"version", "ethernets", "bonds", "bridges", "vlans", "wifis"}
    if set(network_config) - allowed:
        raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_INVALID")
    interfaces = [
        key for key in ("ethernets", "bonds", "bridges", "vlans", "wifis")
        if key in network_config
    ]
    if not interfaces:
        raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_EMPTY")
    for key in interfaces:
        value = network_config[key]
        if not isinstance(value, dict) or not value:
            raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_INVALID")
        if any(
            not isinstance(name, str) or not name or not isinstance(settings, dict)
            for name, settings in value.items()
        ):
            raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_INVALID")
    try:
        encoded = json.dumps(network_config, allow_nan=False)
    except (TypeError, ValueError):
        raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_INVALID") from None
    if len(encoded.encode("utf-8")) > 65536:
        raise EPHSeedError("EPH_SEED_NETWORK_CONFIG_TOO_LARGE")
    return network_config


def _validate_pem(value, field, expected_type):
    if not isinstance(value, str) or len(value) > 131072 or "\x00" in value:
        raise EPHSeedError("EPH_SEED_CREDENTIAL_INVALID:" + field)
    match = PEM_PATTERN.fullmatch(value.strip())
    if match is None or match.group(1) != expected_type:
        raise EPHSeedError("EPH_SEED_CREDENTIAL_INVALID:" + field)
    return value.rstrip() + "\n"


def render_nocloud_seed(config):
    if not isinstance(config, dict):
        raise EPHSeedError("EPH_SEED_CONFIG_INVALID")

    control_url = _require_string(config, "control_url", maximum=2048)
    try:
        parsed_url = urlsplit(control_url)
        parsed_port = parsed_url.port
    except ValueError:
        raise EPHSeedError("EPH_SEED_CONTROL_URL_INVALID") from None
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path not in ("", "/")
        or parsed_url.query
        or parsed_url.fragment
        or (parsed_port is not None and not 1 <= parsed_port <= 65535)
        or any(character.isspace() for character in control_url)
    ):
        raise EPHSeedError("EPH_SEED_CONTROL_URL_INVALID")

    node_id = _require_string(config, "node_id", maximum=64)
    try:
        normalized_node_id = str(uuid.UUID(node_id))
    except (ValueError, TypeError, AttributeError):
        raise EPHSeedError("EPH_SEED_NODE_ID_INVALID") from None
    if normalized_node_id != node_id.lower():
        raise EPHSeedError("EPH_SEED_NODE_ID_INVALID")

    token = _require_string(config, "enrollment_token", minimum=1, maximum=512)
    if not TOKEN_PATTERN.fullmatch(token):
        raise EPHSeedError("EPH_SEED_ENROLLMENT_TOKEN_INVALID")

    agent_image = _require_string(config, "agent_image", maximum=512)
    if not IMAGE_PATTERN.fullmatch(agent_image):
        raise EPHSeedError("EPH_SEED_AGENT_IMAGE_NOT_PINNED")

    hostname = config.get("hostname", "eph-node")
    if not isinstance(hostname, str) or not HOSTNAME_PATTERN.fullmatch(hostname):
        raise EPHSeedError("EPH_SEED_HOSTNAME_INVALID")

    network_config = _validate_network_config(config.get("network_config"))
    ca_certificate = _validate_pem(
        config.get("ca_certificate"), "ca_certificate", "CERTIFICATE"
    )
    client_certificate = _validate_pem(
        config.get("client_certificate"), "client_certificate", "CERTIFICATE"
    )
    client_private_key = _validate_pem(
        config.get("client_private_key"), "client_private_key", "PRIVATE KEY"
    )

    bootstrap_config = {
        "control_url": control_url,
        "node_id": node_id,
        "enrollment_token": token,
        "agent_image": agent_image,
    }
    write_files = [
        {
            "path": "/etc/ai-agent-studio/eph-bootstrap/bootstrap.json",
            "owner": "root:root",
            "permissions": "0600",
            "content": json.dumps(bootstrap_config, indent=2, sort_keys=True),
        },
        {
            "path": "/etc/ai-agent-studio/eph-bootstrap/tls/ca.crt",
            "owner": "root:root",
            "permissions": "0600",
            "content": ca_certificate,
        },
        {
            "path": "/etc/ai-agent-studio/eph-bootstrap/tls/tls.crt",
            "owner": "root:root",
            "permissions": "0600",
            "content": client_certificate,
        },
        {
            "path": "/etc/ai-agent-studio/eph-bootstrap/tls/tls.key",
            "owner": "root:root",
            "permissions": "0600",
            "content": client_private_key,
        },
    ]
    user_data = {
        "hostname": hostname,
        "preserve_hostname": False,
        "ssh_pwauth": False,
        "write_files": write_files,
    }
    metadata = {
        "instance-id": "eph-node-" + node_id,
        "local-hostname": hostname,
    }
    return {
        "user-data": "#cloud-config\n" + json.dumps(user_data, indent=2),
        "meta-data": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        "network-config": json.dumps(network_config, indent=2, sort_keys=True) + "\n",
    }
