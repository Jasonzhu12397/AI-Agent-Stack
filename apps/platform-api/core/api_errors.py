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
