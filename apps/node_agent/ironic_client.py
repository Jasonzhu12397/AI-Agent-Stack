import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any


class IronicObservationError(Exception):
    pass


def ironic_api_configured(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    endpoint = values.get("IRONIC_API_URL", "").strip()
    token = values.get("IRONIC_API_TOKEN", "").strip()
    if bool(endpoint) != bool(token):
        raise IronicObservationError("IRONIC_API_CONFIGURATION_INCOMPLETE")
    return bool(endpoint and token)


def get_ironic_node(
    node_uuid: str,
    environ: Mapping[str, str] | None = None,
    opener: Any = None,
) -> dict[str, Any]:
    values = os.environ if environ is None else environ
    if not ironic_api_configured(values):
        raise IronicObservationError("IRONIC_API_NOT_CONFIGURED")
    if not isinstance(node_uuid, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        node_uuid,
    ):
        raise IronicObservationError("IRONIC_NODE_UUID_INVALID")

    endpoint = values["IRONIC_API_URL"].strip()
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise IronicObservationError("IRONIC_API_URL_INVALID")
    if parsed.path not in ("", "/"):
        raise IronicObservationError("IRONIC_API_URL_PATH_INVALID")

    timeout_text = values.get("IRONIC_API_TIMEOUT_SECONDS", "10")
    try:
        timeout = float(timeout_text)
    except (TypeError, ValueError) as exc:
        raise IronicObservationError("IRONIC_API_TIMEOUT_INVALID") from exc
    if timeout < 1 or timeout > 60:
        raise IronicObservationError("IRONIC_API_TIMEOUT_INVALID")

    headers = {
        "Accept": "application/json",
        "X-Auth-Token": values["IRONIC_API_TOKEN"].strip(),
    }
    microversion = values.get("IRONIC_API_MICROVERSION", "").strip()
    if microversion:
        if not re.fullmatch(r"1\.[0-9]+", microversion):
            raise IronicObservationError("IRONIC_API_MICROVERSION_INVALID")
        headers["X-OpenStack-Ironic-API-Version"] = microversion

    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/nodes/" + urllib.parse.quote(node_uuid, safe=""),
        headers=headers,
        method="GET",
    )
    ca_file = values.get("IRONIC_API_CA_FILE", "").strip()
    try:
        context = ssl.create_default_context(cafile=ca_file or None)
        request_opener = urllib.request.urlopen if opener is None else opener
        with request_opener(request, context=context, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise IronicObservationError(
            "IRONIC_API_HTTP_" + str(exc.code)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IronicObservationError("IRONIC_API_UNAVAILABLE") from exc

    if len(raw) > 1024 * 1024:
        raise IronicObservationError("IRONIC_API_RESPONSE_TOO_LARGE")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IronicObservationError("IRONIC_API_RESPONSE_INVALID_JSON") from exc
    if not isinstance(document, dict):
        raise IronicObservationError("IRONIC_API_RESPONSE_INVALID_SHAPE")
    if document.get("uuid", "").lower() != node_uuid.lower():
        raise IronicObservationError("IRONIC_NODE_IDENTITY_MISMATCH")
    provision_state = document.get("provision_state")
    maintenance = document.get("maintenance")
    target_state = document.get("target_provision_state")
    if not isinstance(provision_state, str) or not isinstance(maintenance, bool):
        raise IronicObservationError("IRONIC_NODE_STATUS_INVALID")
    if target_state is not None and not isinstance(target_state, str):
        raise IronicObservationError("IRONIC_NODE_TARGET_STATE_INVALID")
    return {
        "uuid": node_uuid.lower(),
        "provision_state": provision_state,
        "maintenance": maintenance,
        "target_provision_state": target_state,
        "has_last_error": bool(document.get("last_error")),
        "has_maintenance_reason": bool(document.get("maintenance_reason")),
    }
