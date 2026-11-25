"""One JSON error contract for every API route.

DRF's exception handler covers failures raised inside a view, but several
layers can still answer before or outside it:

* Django's CSRF middleware returns an HTML 403 for an expired token;
* the resolver returns an HTML 404 for an unknown ``/api/`` route;
* ``DEBUG`` turns an uncaught exception into an HTML traceback page;
* a handful of legacy views return ``{"detail": "..."}`` directly.

The browser cannot classify those consistently. This middleware normalizes
only API error responses; successful responses, health probes and the console
bundle are untouched.
"""

from __future__ import annotations

import json
import logging

from django.http import JsonResponse

from core.api_errors import STATUS_ERROR_CODES

logger = logging.getLogger(__name__)

API_PREFIX = "/api/"
_MESSAGES = {
    400: "The request could not be processed.",
    401: "Authentication required.",
    403: "The request was rejected before the API view ran. If this is a browser "
    "session, reload the page to refresh the CSRF cookie and sign in again.",
    404: "The requested API route was not found.",
    405: "This HTTP method is not allowed for the requested API route.",
    409: "The request conflicts with the current state.",
    429: "Too many requests; retry later.",
    500: "The server could not complete the request.",
    502: "The platform could not reach an upstream service.",
    503: "The service is temporarily unavailable.",
}


def _is_api(request) -> bool:
    return request.path.startswith(API_PREFIX)


def _is_json(response) -> bool:
    return "json" in (response.get("Content-Type") or "").lower()


def _json_error(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": code, "message": message, "details": {}}},
        status=status,
    )


def _message_for(status: int) -> str:
    return _MESSAGES.get(status, f"The request failed with HTTP {status}.")


def _envelope_payload(response):
    """Read a rendered JSON error and replace its shape with the contract."""

    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("error"), dict):
        return None
    detail = payload.get("detail")
    message = detail if isinstance(detail, str) and detail else payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = _message_for(response.status_code)
    return {"error": {"code": STATUS_ERROR_CODES.get(response.status_code, "ERROR"),
                      "message": message.strip(), "details": {}}}


class APIContractMiddleware:
    """Normalize non-contract API errors at the outermost response boundary."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
        except Exception:
            if not _is_api(request):
                raise
            logger.exception("Unhandled API exception on %s", request.path)
            return _json_error("SERVER_ERROR", _message_for(500), 500)
        return self.process_response(request, response)

    def process_response(self, request, response):
        if not _is_api(request) or response.status_code < 400:
            return response
        if _is_json(response):
            replaced = _envelope_payload(response)
            if replaced is None:
                return response
            response.content = json.dumps(replaced, separators=(",", ":")).encode()
            response["Content-Length"] = str(len(response.content))
            response["Content-Type"] = "application/json"
            return response
        # An HTML error from CSRF, the resolver or Django's DEBUG page must not
        # reach the console as a "network failure" it cannot classify.
        code = STATUS_ERROR_CODES.get(response.status_code, "ERROR")
        replacement = _json_error(code, _message_for(response.status_code), response.status_code)
        allow = response.get("Allow")
        if allow:
            replacement["Allow"] = allow
        return replacement
