"""Shared contract error-envelope handling for settings-center HTTP views.

The frozen contract (``docs/settings-center-contract.md``) requires every
error response to use a single envelope::

    {"error": {"code": "UPPER_SNAKE_CODE", "message": "human readable",
               "details": {"field": ["reason"]}}}

This module provides the envelope response helper and a mixin that maps
DRF/Django exceptions onto that envelope. Views in ``billing_views``,
``data_controls_views`` and ``connector_settings_views`` inherit the mixin so
anonymous, forbidden and unknown-scope responses are shaped identically to
the ``SettingsBaseView`` family.

Exception → envelope mapping:

| Exception                    | HTTP | ``error.code``                |
|------------------------------|------|-------------------------------|
| ``NotAuthenticated``         | 401  | ``AUTHENTICATION_REQUIRED``   |
| ``AuthenticationFailed``     | 401  | ``AUTHENTICATION_FAILED``      |
| ``PermissionDenied``         | 403  | ``PERMISSION_DENIED``         |
| ``NotFound`` / ``Http404``   | 404  | ``NOT_FOUND``                  |
| other ``APIException``       | its status | ``default_code`` upper-cased |
| unexpected exception         | 500  | re-raised; internal details are not echoed |
"""

from __future__ import annotations

from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
)
from rest_framework.response import Response


def error_envelope_response(
    code: str,
    message: str,
    details: dict | None = None,
    http_status: int = status.HTTP_400_BAD_REQUEST,
) -> Response:
    """Build a contract-envelope ``Response``."""

    return Response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=http_status,
    )


def _normalize_detail(value: object) -> object:
    if isinstance(value, list):
        return [str(item) for item in value]
    return str(value)


def _extract_details(exc: APIException) -> dict[str, object]:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return {key: _normalize_detail(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return {"non_field_errors": [str(item) for item in detail]}
    return {}


#: Stable codes for the legacy views that only said "this failed". The HTTP
#: status carries the class of failure, ``message`` keeps the specific reason,
#: so a client can branch on ``error.code`` without a per-message catalogue.
STATUS_ERROR_CODES = {
    400: "INVALID_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "STATE_CONFLICT",
    422: "UNPROCESSABLE",
    429: "RATE_LIMITED",
    500: "SERVER_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def legacy_error_response(
    message: str,
    http_status: int = 400,
    code: str | None = None,
    extra: dict | None = None,
) -> Response:
    """Answer an older view with the shared envelope.

    These views used to return ``{"detail": message}``. The status still
    classifies the failure, so the code is derived from it unless the view
    knows a sharper one; ``message`` is preserved verbatim because callers and
    tests rely on its wording.

    ``extra`` keeps the structured fields the browser already reads (network
    readiness, drift, inventory) at the top level next to the envelope, so the
    change is additive for clients instead of reshaping their payload.
    """

    resolved = code or STATUS_ERROR_CODES.get(http_status, "ERROR")
    if extra:
        payload = {**extra, "error": {"code": resolved, "message": message, "details": {}}}
        return Response(payload, status=http_status)
    return error_envelope_response(resolved, message, http_status=http_status)


class ErrorEnvelopeMixin:
    """Map DRF/Django exceptions onto the frozen contract error envelope.

    Mix this into an ``APIView`` subclass. Permission/auth classes are owned by
    the view; this mixin only rewrites the error response shape.
    """

    unauthenticated_code = "AUTHENTICATION_REQUIRED"
    authentication_failed_code = "AUTHENTICATION_FAILED"
    permission_denied_code = "PERMISSION_DENIED"
    not_found_code = "NOT_FOUND"

    unauthenticated_message = "Authentication required."
    authentication_failed_message = "Authentication failed."
    permission_denied_message = "Permission denied."
    not_found_message = "Resource not found."
    api_exception_message = "Request could not be processed."

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, Http404):
            # Generic message: never disclose whether the object exists.
            return error_envelope_response(
                self.not_found_code,
                self.not_found_message,
                {},
                status.HTTP_404_NOT_FOUND,
            )
        if isinstance(exc, NotAuthenticated):
            return error_envelope_response(
                self.unauthenticated_code,
                self.unauthenticated_message,
                {},
                status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, AuthenticationFailed):
            return error_envelope_response(
                self.authentication_failed_code,
                self.authentication_failed_message,
                {},
                status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, PermissionDenied):
            return error_envelope_response(
                self.permission_denied_code,
                self.permission_denied_message,
                {},
                status.HTTP_403_FORBIDDEN,
            )
        if isinstance(exc, NotFound):
            return error_envelope_response(
                self.not_found_code,
                self.not_found_message,
                {},
                status.HTTP_404_NOT_FOUND,
            )
        if isinstance(exc, APIException):
            code = (getattr(exc, "default_code", None) or "error").upper()
            details = _extract_details(exc)
            detail = getattr(exc, "detail", None)
            if isinstance(detail, (dict, list)):
                message = self.api_exception_message
            else:
                message = str(detail) if detail is not None else self.api_exception_message
            return error_envelope_response(
                code,
                message,
                details,
                exc.status_code,
            )
        # Unexpected exception: defer to DRF, which re-raises it. The 500
        # response must not echo internal details or tracebacks.
        return super().handle_exception(exc)


def contract_exception_handler(exc, context):
    """Map framework-level exceptions onto the shared envelope.

    Views that raise ``MethodNotAllowed``, ``ParseError`` or ``NotAcceptable``
    never reach their own error handling, so the response used to carry DRF's
    translated ``{"detail": ...}``. Installed as ``EXCEPTION_HANDLER`` so every
    endpoint answers in one shape regardless of how the failure escaped.
    """

    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(response.data, dict) and "error" in response.data:
        return response
    if isinstance(exc, APIException):
        generic = {
            "invalid",
            "parse_error",
            "not_acceptable",
            "unsupported_media_type",
            "method_not_allowed",
            "not_found",
            "permission_denied",
            "not_authenticated",
            "authentication_failed",
            "throttled",
        }
        default_code = (getattr(exc, "default_code", None) or "error").lower()
        # Validation, parse and method failures share one stable code per
        # status so a client sees INVALID_REQUEST rather than INVALID.
        if default_code in generic:
            code = STATUS_ERROR_CODES.get(response.status_code, default_code.upper())
        else:
            code = default_code.upper()
        details = _extract_details(exc)
        detail = getattr(exc, "detail", None)
        if isinstance(detail, (dict, list)):
            message = "Request could not be processed."
        else:
            message = str(detail) if detail is not None else "Request could not be processed."
        return error_envelope_response(code, message, details, response.status_code)
    return response
