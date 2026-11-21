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
    "dhcp": "dhcp",
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
    }
    # A DHCP-addressed provisioning link obtains its own address from the
    # network, so demanding a static one here would force callers to invent an
    # address (and they used to invent the gateway's).
    if networking_value.get("dhcp") is not True:
        networking["ipAddress"] = _required_string(
            networking_value.get("ip_address"),
            "IRONIC_IP_ADDRESS_REQUIRED",
        )
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


#: How a template provenance label maps back to the networking key it filled.
_TEMPLATE_SOURCE_KEYS = {
    "interface": "interface",
    "ip_address": "ip_address",
    "ip_address_subnet": "ip_address",
    "dhcp": "dhcp",
}


def apply_provisioning_defaults(
    ironic: Any,
    defaults: dict[str, Any],
    sources: list[str] | tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Fill only absent networking keys and report where the values came from.

    ``sources`` is the provenance the template reported. Passing it keeps the
    label honest (an address derived from the subnet stays
    ``ip_address_subnet``) instead of relabelling every filled key ``ip_address``.
    """

    if not isinstance(ironic, dict):
        return ironic, []
    networking = ironic.get("networking")
    if networking is None:
        networking = {}
    if not isinstance(networking, dict):
        return ironic, []
    filled = dict(networking)
    filled_keys: list[str] = []
    for key in ("interface", "ip_address", "dhcp"):
        if filled.get(key) not in (None, "", False):
            continue
        value = defaults.get(key)
        if value in (None, ""):
            continue
        filled[key] = value
        filled_keys.append(key)
    if not filled_keys:
        return ironic, []
    if sources is None:
        used = [key for key in filled_keys if key in ("interface", "ip_address")]
    else:
        used = [
            label
            for label in sources
            if _TEMPLATE_SOURCE_KEYS.get(label) in filled_keys
        ]
    return {**ironic, "networking": filled}, used


# Kept for callers that only need the resolved networking block.
def resolve_provisioning_networking(
    ironic: Any,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    resolved, _ = apply_provisioning_defaults(ironic, defaults)
    return resolved
