"""The API must be able to serve the built web console on its own origin.

Deployments expose a single port: the API process returns the console bundle and
falls back to ``index.html`` for client-side routes, so a browser never needs a
second server, cross-origin rules, or a proxy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

INDEX_MARKER = "<title>console-under-test</title>"


def _write_bundle(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(f"<!doctype html><html><head>{INDEX_MARKER}</head></html>")
    (root / "assets" / "app.js").write_text("console.log('bundle');")


@pytest.fixture
def bundle(tmp_path) -> Path:
    root = tmp_path / "dist"
    _write_bundle(root)
    return root


@pytest.mark.django_db
def test_root_serves_the_bundle(bundle):
    with override_settings(CONSOLE_DIST_DIR=str(bundle)):
        response = APIClient().get("/")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert INDEX_MARKER in response.content.decode()


@pytest.mark.django_db
def test_client_side_route_falls_back_to_index(bundle):
    with override_settings(CONSOLE_DIST_DIR=str(bundle)):
        response = APIClient().get("/settings/subscription")
    assert response.status_code == 200
    assert INDEX_MARKER in response.content.decode()


@pytest.mark.django_db
def test_asset_is_served_from_the_bundle(bundle):
    with override_settings(CONSOLE_DIST_DIR=str(bundle)):
        response = APIClient().get("/assets/app.js")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/javascript")


@pytest.mark.django_db
def test_asset_traversal_is_rejected(bundle):
    outside = bundle.parent / "secret.txt"
    outside.write_text("do-not-serve")
    with override_settings(CONSOLE_DIST_DIR=str(bundle)):
        response = APIClient().get("/assets/%2e%2e/secret.txt")
    assert response.status_code == 404
    assert b"do-not-serve" not in response.content


@pytest.mark.django_db
def test_api_routes_are_not_swallowed_by_the_fallback(bundle):
    with override_settings(CONSOLE_DIST_DIR=str(bundle)):
        response = APIClient().get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert INDEX_MARKER not in response.content.decode()


@pytest.mark.django_db
def test_missing_bundle_reports_how_to_build(tmp_path):
    with override_settings(CONSOLE_DIST_DIR=str(tmp_path / "empty")):
        response = APIClient().get("/")
    assert response.status_code == 503
    assert "bootstrap.py" in response.content.decode()
