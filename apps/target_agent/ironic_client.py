from __future__ import annotations

import collections.abc
import json
import math
import os
import ssl
import typing
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_IRONIC_RESPONSE_BYTES = 1024 * 1024


class IronicObservationError(ValueError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def ironic_api_configured(environ: typing.Any = None) -> bool:
    values = os.environ if environ is None else environ
    has_endpoint = bool(values.get("IRONIC_API_URL"))
    has_token = bool(values.get("IRONIC_API_TOKEN"))
    if not has_endpoint and not has_token:
        return False
    if not has_endpoint or not has_token:
        raise IronicObservationError("IRONIC_API_CONFIGURATION_INCOMPLETE")
    _configuration(values)
    return True


def _configuration(
    environ: collections.abc.Mapping[str, str],
) -> tuple[str, str, str, float]:
    endpoint = environ.get("IRONIC_API_URL", "").strip()
    token = environ.get("IRONIC_API_TOKEN", "")
    microversion = environ.get("IRONIC_API_MICROVERSION", "1.82").strip()
    if not endpoint or not token:
        raise IronicObservationError("IRONIC_API_CONFIGURATION_INCOMPLETE")
    if any(character in token for character in ("\r", "\n", "\\r", "\\n")):
        raise IronicObservationError("IRONIC_API_TOKEN_INVALID")
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
    try:
        timeout = float(environ.get("IRONIC_API_TIMEOUT_SECONDS", "10"))
    except (TypeError, ValueError) as exc:
        raise IronicObservationError("IRONIC_API_TIMEOUT_INVALID") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise IronicObservationError("IRONIC_API_TIMEOUT_INVALID")
    if not microversion or any(character in microversion for character in ("\r", "\n")):
        raise IronicObservationError("IRONIC_API_MICROVERSION_INVALID")
    return endpoint.rstrip("/"), token, microversion, timeout


def get_ironic_node(
    node_uuid: str,
    environ: collections.abc.Mapping[str, str] | None = None,
    opener: typing.Any = None,
) -> dict[str, typing.Any]:
    try:
        normalized_uuid = str(uuid.UUID(node_uuid))
    except (TypeError, ValueError, AttributeError) as exc:
        raise IronicObservationError("IRONIC_NODE_UUID_INVALID") from exc
    if normalized_uuid != node_uuid.lower():
        raise IronicObservationError("IRONIC_NODE_UUID_INVALID")
    values = os.environ if environ is None else environ
    endpoint, token, microversion, timeout = _configuration(values)
    request = urllib.request.Request(
        f"{endpoint}/v1/nodes/{normalized_uuid}",
        headers={
            "Accept": "application/json",
            "X-Auth-Token": token,
            "X-OpenStack-Ironic-API-Version": microversion,
        },
        method="GET",
    )
    context = ssl.create_default_context()
    try:
        if opener is None:
            active_opener = urllib.request.build_opener(
                _NoRedirectHandler(),
                urllib.request.HTTPSHandler(context=context),
            )
            response = active_opener.open(request, timeout=timeout)
        else:
            response = opener(
                request,
                context=context,
                timeout=timeout,
            )
        with response:
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise IronicObservationError("IRONIC_API_CONTENT_TYPE_INVALID")
            raw = response.read(MAX_IRONIC_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise IronicObservationError(f"IRONIC_API_HTTP_{exc.code}") from None
    except IronicObservationError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IronicObservationError("IRONIC_API_UNAVAILABLE") from exc
    if len(raw) > MAX_IRONIC_RESPONSE_BYTES:
        raise IronicObservationError("IRONIC_API_RESPONSE_TOO_LARGE")
    try:
        document = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise IronicObservationError("IRONIC_API_RESPONSE_INVALID_JSON") from exc
    if not isinstance(document, dict):
        raise IronicObservationError("IRONIC_API_RESPONSE_INVALID_OBJECT")
    returned_uuid = document.get("uuid")
    if not isinstance(returned_uuid, str) or not returned_uuid:
        raise IronicObservationError("IRONIC_NODE_IDENTITY_INVALID")
    if returned_uuid != normalized_uuid:
        raise IronicObservationError("IRONIC_NODE_IDENTITY_MISMATCH")
    provision_state = document.get("provision_state")
    maintenance = document.get("maintenance")
    if not isinstance(provision_state, str) or not provision_state:
        raise IronicObservationError("IRONIC_NODE_STATE_INVALID")
    if not isinstance(maintenance, bool):
        raise IronicObservationError("IRONIC_NODE_MAINTENANCE_STATE_INVALID")
    target_state = document.get("target_provision_state")
    if target_state is not None and not isinstance(target_state, str):
        raise IronicObservationError("IRONIC_NODE_TARGET_STATE_INVALID")
    return {
        "uuid": normalized_uuid,
        "provision_state": provision_state,
        "maintenance": maintenance,
        "target_provision_state": target_state,
        "last_error": document.get("last_error"),
        "maintenance_reason": document.get("maintenance_reason"),
    }
