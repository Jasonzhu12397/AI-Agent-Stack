"""Render the Ironic workload CR the EPH owns.

The Ironic Standalone Operator Deployment shipped offline is not enough: the
operator only reconciles an ``Ironic`` resource, and that resource carries every
environment specific value (images, provisioning interface, service address,
credentials Secret). The platform therefore supplies those values in the
deployment spec and this module turns them into the CR, failing closed when a
required value is absent instead of inventing one.

Nothing here talks to a cluster; the caller applies the returned document.
"""

from __future__ import annotations

from typing import Any

CR_NAMESPACE_DEFAULT = "baremetal-operator-system"
CR_NAME = "ironic"
API_VERSION = "ironic.metal3.io/v1alpha1"
IRONIC_CRD_NAME = "ironics.ironic.metal3.io"

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


class IronicRenderError(ValueError):
    """Raised when the deployment spec cannot produce a valid Ironic CR."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _required_string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IronicRenderError(code)
    if len(value) > 512:
        raise IronicRenderError(code)
    return value.strip()


def render_ironic_cr(spec: Any) -> dict[str, Any] | None:
    """Return the ``Ironic`` CR implied by ``spec.ironic``, or ``None``.

    ``None`` means the caller did not configure the workload here and must
    supply the CR through its own manifest source.
    """

    if not isinstance(spec, dict):
        raise IronicRenderError("IRONIC_SPEC_INVALID")
    ironic = spec.get("ironic")
    if ironic is None:
        return None
    if not isinstance(ironic, dict):
        raise IronicRenderError("IRONIC_SPEC_INVALID")

    namespace = ironic.get("namespace", CR_NAMESPACE_DEFAULT)
    if namespace != CR_NAMESPACE_DEFAULT:
        # The control-stack readiness probe observes this exact namespace.
        raise IronicRenderError("IRONIC_NAMESPACE_UNSUPPORTED")

    images_value = ironic.get("images")
    if not isinstance(images_value, dict):
        raise IronicRenderError("IRONIC_IMAGES_REQUIRED")
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
        raise IronicRenderError("IRONIC_NETWORKING_REQUIRED")
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
            raise IronicRenderError("IRONIC_NETWORKING_INVALID")
        networking[cr_key] = value
    for key, cr_key in _NETWORK_BOOLEAN_KEYS.items():
        value = networking_value.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise IronicRenderError("IRONIC_NETWORKING_INVALID")
        networking[cr_key] = value
    mac_addresses = networking_value.get("mac_addresses")
    if mac_addresses is not None:
        if (
            not isinstance(mac_addresses, list)
            or not 1 <= len(mac_addresses) <= 16
            or any(
                not isinstance(item, str) or not item.strip() for item in mac_addresses
            )
        ):
            raise IronicRenderError("IRONIC_NETWORKING_INVALID")
        networking["macAddresses"] = [item.strip() for item in mac_addresses]

    return {
        "apiVersion": API_VERSION,
        "kind": "Ironic",
        "metadata": {"name": CR_NAME, "namespace": namespace},
        "spec": {
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
        },
    }


def contains_ironic_cr(documents: Any) -> bool:
    """True when a manifest source already carries an ``Ironic`` resource."""

    if not isinstance(documents, list):
        return False
    return any(
        isinstance(document, dict)
        and document.get("kind") == "Ironic"
        and document.get("apiVersion") == API_VERSION
        for document in documents
    )
