"""Serve the built web console from the API process.

Hosting the console on the API origin keeps a deployment to a single public
port: no CORS, no second runtime, and session cookies stay first-party. The
bundle is produced by the frontend build and only read here.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse
from django.views.decorators.http import require_GET

MISSING_BUNDLE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>AI Agent Stack</title></head>'
    '<body style="font-family:system-ui;padding:48px;line-height:1.6">'
    "<h1>Web console bundle is missing</h1>"
    "<p>The API is running, but the frontend has not been built yet.</p>"
    "<pre>python3 .devcontainer/bootstrap.py</pre>"
    "</body></html>"
)


def console_dir() -> Path:
    """Directory holding the built console, overridable for tests and deploys."""

    configured = getattr(settings, "CONSOLE_DIST_DIR", None)
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "apps" / "web-studio" / "dist"


def _served(request_path: str) -> Path:
    root = (console_dir() / "assets").resolve()
    candidate = (root / request_path.lstrip("/")).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("console asset escapes the bundle directory")
    return candidate


@require_GET
def console_index(request, path: str = "") -> HttpResponse:
    index = console_dir() / "index.html"
    if not index.is_file():
        return HttpResponse(MISSING_BUNDLE, status=503, content_type="text/html; charset=utf-8")
    response = HttpResponse(index.read_bytes(), content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def console_asset(request, path: str) -> HttpResponse:
    try:
        target = _served(path)
    except ValueError:
        return HttpResponse(status=404)
    if not target.is_file():
        return HttpResponse(status=404)
    return FileResponse(target.open("rb"))
