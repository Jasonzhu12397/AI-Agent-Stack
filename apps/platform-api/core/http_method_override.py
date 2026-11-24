"""Method override so write verbs survive HTTP proxies.

Some hosting front doors (the Codespaces port proxy is one) reject ``DELETE``
and ``PATCH`` with an empty 403 before the request ever reaches Django. Clients
therefore send ``POST`` with ``X-HTTP-Method-Override``; this middleware
rewrites the request back to the intended verb ahead of CSRF validation, so the
view, the permission checks and the CSRF token all behave exactly as before.
"""

from __future__ import annotations

ALLOWED_OVERRIDES = frozenset({"DELETE", "PATCH", "PUT"})


class MethodOverrideMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        override = (request.headers.get("X-HTTP-Method-Override") or "").strip().upper()
        if override in ALLOWED_OVERRIDES and request.method == "POST":
            request.method = override
        return self.get_response(request)
