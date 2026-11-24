"""Trigger a one-time PXE boot on a bare-metal BMC through Redfish.

The EPH installs Ironic, but the machines still have to be told to boot from
the network before any inspection or provisioning can happen. This module is
the vendor-neutral Redfish part: read the computer system, verify PXE is
advertised, set a one-time boot override and reset the host.

Planning and execution are separate. A plan performs only GET requests; execution
needs an explicit confirmation from the API layer. Everything is per machine so
one broken BMC cannot hide the rest of the fleet.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx


class PxeBootError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_SYSTEM_PATH = re.compile(r"^/redfish/v1/Systems/[A-Za-z0-9_.-]{1,64}$")
_BOOT_TARGET = "Pxe"
_MAX_DOCUMENT_BYTES = 1048576


def _private_endpoint(endpoint: str) -> tuple[str, str]:
    parsed = urlsplit(endpoint or "")
    hostname = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise PxeBootError("PXE_BOOT_BMC_ADDRESS_INVALID") from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise PxeBootError("PXE_BOOT_BMC_ADDRESS_INVALID")
    return f"https://{parsed.netloc}", hostname


def _json_document(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise PxeBootError("PXE_BOOT_RESPONSE_INVALID")
    if len(response.content) > _MAX_DOCUMENT_BYTES:
        raise PxeBootError("PXE_BOOT_RESPONSE_INVALID")
    try:
        document = response.json()
    except ValueError as exc:
        raise PxeBootError("PXE_BOOT_RESPONSE_INVALID") from exc
    if not isinstance(document, dict):
        raise PxeBootError("PXE_BOOT_RESPONSE_INVALID")
    return document


def _request(client: httpx.Client, method: str, url: str, auth, document=None) -> dict[str, Any]:
    try:
        response = client.request(method, url, auth=auth, json=document)
    except httpx.HTTPError as exc:
        raise PxeBootError("PXE_BOOT_REQUEST_FAILED") from exc
    if response.status_code in (401, 403):
        raise PxeBootError("PXE_BOOT_AUTH_FAILED")
    if 300 <= response.status_code < 400:
        raise PxeBootError("PXE_BOOT_REDIRECT_REFUSED")
    if response.status_code >= 400:
        raise PxeBootError("PXE_BOOT_REQUEST_FAILED")
    if not response.content:
        return {}
    return _json_document(response)


def read_system(client: httpx.Client, base_url: str, auth) -> tuple[str, dict[str, Any]]:
    collection = _request(client, "GET", f"{base_url}/redfish/v1/Systems", auth)
    members = collection.get("Members")
    if not isinstance(members, list) or len(members) != 1:
        raise PxeBootError("PXE_BOOT_SYSTEM_MEMBER_COUNT_INVALID")
    path = members[0].get("@odata.id") if isinstance(members[0], dict) else None
    if not isinstance(path, str) or not _SYSTEM_PATH.fullmatch(path):
        raise PxeBootError("PXE_BOOT_SYSTEM_PATH_INVALID")
    system = _request(client, "GET", f"{base_url}{path}", auth)
    return path, system


def build_pxe_boot_plan(system: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(system, dict):
        raise PxeBootError("PXE_BOOT_SYSTEM_INVALID")
    boot = system.get("Boot")
    if not isinstance(boot, dict):
        raise PxeBootError("PXE_BOOT_UNSUPPORTED")
    supported = boot.get("BootSourceOverrideSupported")
    if not isinstance(supported, list):
        raise PxeBootError("PXE_BOOT_UNSUPPORTED")
    normalized = {str(item).casefold() for item in supported if isinstance(item, str)}
    if "pxe" not in normalized:
        raise PxeBootError("PXE_BOOT_UNSUPPORTED")
    power_state = system.get("PowerState")
    if not isinstance(power_state, str) or not power_state:
        raise PxeBootError("PXE_BOOT_POWER_STATE_UNKNOWN")
    reset_type = "On" if power_state.casefold() == "off" else "ForceRestart"
    return {
        "boot_patch": {
            "Boot": {
                "BootSourceOverrideTarget": _BOOT_TARGET,
                "BootSourceOverrideEnabled": "Once",
            }
        },
        "reset_type": reset_type,
        "power_state": power_state,
        "supported_targets": sorted(normalized),
    }


def apply_pxe_boot(
    client: httpx.Client,
    base_url: str,
    auth,
    system_path: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    _request(client, "PATCH", f"{base_url}{system_path}", auth, plan["boot_patch"])
    result = _request(
        client,
        "POST",
        f"{base_url}{system_path}/Actions/ComputerSystem.Reset",
        auth,
        {"ResetType": plan["reset_type"]},
    )
    return {
        "boot_override": "Pxe/Once",
        "reset_type": plan["reset_type"],
        "reset_response": result,
    }


def pxe_boot_machine(
    *,
    endpoint: str,
    credential: dict[str, str],
    trust_bundle_pem: str = "",
    execute: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Plan or execute one PXE boot. Only ``execute=True`` changes the BMC."""

    base_url, _hostname = _private_endpoint(endpoint)
    username = credential.get("username") if isinstance(credential, dict) else None
    password = credential.get("password") if isinstance(credential, dict) else None
    if (
        not isinstance(username, str)
        or not username
        or not isinstance(password, str)
        or not password
    ):
        raise PxeBootError("PXE_BOOT_CREDENTIAL_INVALID")
    ssl_context = ssl.create_default_context(cadata=trust_bundle_pem or None)
    auth = httpx.BasicAuth(username, password)
    with httpx.Client(
        verify=ssl_context,
        timeout=httpx.Timeout(10.0, connect=3.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        system_path, system = read_system(client, base_url, auth)
        plan = build_pxe_boot_plan(system)
        evidence = {
            "available": True,
            "execute": bool(execute),
            "system_path": system_path,
            "system_uuid": system.get("UUID"),
            "manufacturer": system.get("Manufacturer"),
            "model": system.get("Model"),
            **plan,
        }
        if execute:
            evidence.update(apply_pxe_boot(client, base_url, auth, system_path, plan))
        return evidence
