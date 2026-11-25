"""Every API error must leave as JSON, even outside a DRF view."""

import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_unknown_api_route_is_a_json_envelope_not_an_html_404():
    response = APIClient().get("/api/v1/definitely-not-a-route/")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "The requested API route was not found."
    assert "detail" not in body


@pytest.mark.django_db
def test_legacy_detail_error_is_normalized_for_the_browser():
    response = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "missing", "password": "wrong"},
        format="json",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["message"] == "Invalid username or password."
    assert "detail" not in body


@pytest.mark.django_db
def test_csrf_rejection_is_a_json_403_not_the_django_html_page():
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    response = client.post(
        "/api/v1/auth/login/",
        data='{"username": "u", "password": "p"}',
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "PERMISSION_DENIED"
    assert "reload the page" in body["error"]["message"]


@pytest.mark.django_db
def test_health_probe_keeps_its_existing_shape():
    response = APIClient().get("/health/ready")

    assert response.status_code in {200, 503}
    body = response.json()
    assert "error" not in body
    assert body["status"] in {"ready", "not_ready"}
