"""Single source of truth for the Ironic workload contract.

The platform validates a deployment spec before dispatch, the orchestration
contract forwards it, and the EPH renders the ``Ironic`` custom resource from
it. Those three callers must agree on what a valid configuration is, so the
rules live here rather than in any one application.

The normaliser never fills in a missing value: every absent required key is a
distinct error code so the caller can fail closed and report the exact gap.
"""

from __future__ import annotations

from typing import Any

CR_NAMESPACE = "baremetal-operator-system"
CR_NAME = "ironic"
API_VERSION = "ironic.metal3.io/v1alpha1"
CRD_NAME = "ironics.ironic.metal3.io"

_REQUIRED_IMAGES = ("ironic", "deploy_ramdisk_downloader", "keepalived")
_IMAGE_KEYS = {
    "ironic": "ironic",
    "deploy_ramdisk_downloader": "deployRamdiskDownloader",
    "keepalived": "keepalived",
    "deploy_ramdisk_branch": "deployRamdiskBranch",
}
_NETWORK_STRING_KEYS = {
    "interface": "interface",
    "ip_address": "ipAddress",
    "external_ip": "externalIP",
    "external_callback_url": "externalCallbackURL",
    "image_server_external_url": "imageServerExternalURL",
    "ip_address_manager": "ipAddressManager",
}
_NETWORK_INTEGER_KEYS = {
    "api_port": "apiPort",
    "rpc_port": "rpcPort",
    "image_server_port": "imageServerPort",
    "image_server_tls_port": "imageServerTLSPort",
}
_NETWORK_BOOLEAN_KEYS = {
    "disable_host_network": "disableHostNetwork",
    "bind_interface": "bindInterface",
}


class IronicSpecError(ValueError):
    """Raised when an Ironic configuration cannot be normalised."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _required_string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise IronicSpecError(code)
    return value.strip()


def normalize_ironic_config(ironic: Any) -> dict[str, Any]:
    """Return the camelCase ``Ironic`` spec for a platform configuration.

    ``ironic`` must be a mapping; callers decide whether the section is
    optional before calling this.
    """

    if not isinstance(ironic, dict):
        raise IronicSpecError("IRONIC_SPEC_INVALID")

    namespace = ironic.get("namespace", CR_NAMESPACE)
    if namespace != CR_NAMESPACE:
        # The control-stack readiness probe observes this exact namespace.
        raise IronicSpecError("IRONIC_NAMESPACE_UNSUPPORTED")

    images_value = ironic.get("images")
    if not isinstance(images_value, dict):
        raise IronicSpecError("IRONIC_IMAGES_REQUIRED")
    images: dict[str, str] = {}
    for key, cr_key in _IMAGE_KEYS.items():
        if key in _REQUIRED_IMAGES:
            images[cr_key] = _required_string(
                images_value.get(key),
                "IRONIC_IMAGE_REQUIRED",
            )
        elif images_value.get(key) is not None:
            images[cr_key] = _required_string(
                images_value.get(key),
                "IRONIC_IMAGE_INVALID",
            )

    networking_value = ironic.get("networking")
    if not isinstance(networking_value, dict):
        raise IronicSpecError("IRONIC_NETWORKING_REQUIRED")
    networking: dict[str, Any] = {
        "interface": _required_string(
            networking_value.get("interface"),
            "IRONIC_INTERFACE_REQUIRED",
        ),
        "ipAddress": _required_string(
            networking_value.get("ip_address"),
            "IRONIC_IP_ADDRESS_REQUIRED",
        ),
    }
    for key, cr_key in _NETWORK_STRING_KEYS.items():
        if key in networking:
            continue
        if networking_value.get(key) is not None:
            networking[cr_key] = _required_string(
                networking_value.get(key),
                "IRONIC_NETWORKING_INVALID",
            )
    for key, cr_key in _NETWORK_INTEGER_KEYS.items():
        value = networking_value.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise IronicSpecError("IRONIC_NETWORKING_INVALID")
        networking[cr_key] = value
    for key, cr_key in _NETWORK_BOOLEAN_KEYS.items():
        value = networking_value.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise IronicSpecError("IRONIC_NETWORKING_INVALID")
        networking[cr_key] = value
    mac_addresses = networking_value.get("mac_addresses")
    if mac_addresses is not None:
        if (
            not isinstance(mac_addresses, list)
            or not 1 <= len(mac_addresses) <= 16
            or any(not isinstance(item, str) or not item.strip() for item in mac_addresses)
        ):
            raise IronicSpecError("IRONIC_NETWORKING_INVALID")
        networking["macAddresses"] = [item.strip() for item in mac_addresses]

    return {
        "apiCredentialsName": _required_string(
            ironic.get("api_credentials_name"),
            "IRONIC_CREDENTIALS_REQUIRED",
        ),
        "version": _required_string(
            ironic.get("version"),
            "IRONIC_VERSION_REQUIRED",
        ),
        "images": images,
        "networking": networking,
    }


def provisioning_defaults(network: Any) -> dict[str, str]:
    """Return the Ironic networking values implied by a provisioning network.

    Only values the profile actually declares are returned: the link is the
    bond when present, and the address is the network's declared gateway. The
    gateway is a prefill default (an EPH host usually owns it on the
    provisioning segment) and stays editable; the caller must not treat it as
    authoritative when the profile leaves it unset.
    """

    if not isinstance(network, dict):
        return {}
    defaults: dict[str, str] = {}
    bond = network.get("bond")
    if isinstance(bond, dict) and isinstance(bond.get("name"), str) and bond["name"].strip():
        defaults["interface"] = bond["name"].strip()
    elif isinstance(network.get("interface"), str) and network["interface"].strip():
        defaults["interface"] = network["interface"].strip()
    gateway = network.get("gateway_ipv4")
    if isinstance(gateway, str) and gateway.strip():
        defaults["ip_address"] = gateway.strip()
    return defaults


def apply_provisioning_defaults(
    ironic: Any,
    defaults: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Fill only absent networking keys and report which keys came from defaults."""

    if not isinstance(ironic, dict):
        return ironic, []
    networking = ironic.get("networking")
    if networking is None:
        networking = {}
    if not isinstance(networking, dict):
        return ironic, []
    filled = dict(networking)
    sources: list[str] = []
    if not filled.get("interface") and defaults.get("interface"):
        filled["interface"] = defaults["interface"]
        sources.append("interface")
    if not filled.get("ip_address") and defaults.get("ip_address"):
        filled["ip_address"] = defaults["ip_address"]
        sources.append("ip_address")
    if not sources:
        return ironic, []
    return {**ironic, "networking": filled}, sources
