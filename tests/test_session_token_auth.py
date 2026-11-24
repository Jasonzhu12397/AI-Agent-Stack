"""The console keeps working when a proxy drops the session cookie."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


def _login(client):
    csrf = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login/",
        {"username": "token-user", "password": "strong-test-password"},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert response.status_code == 200
    return response.json()["session_token"]


@pytest.mark.django_db
def test_login_returns_the_session_key_as_a_token():
    get_user_model().objects.create_user(
        username="token-user",
        email="token@example.test",
        password="strong-test-password",
    )
    token = _login(APIClient(enforce_csrf_checks=True))
    assert isinstance(token, str) and len(token) >= 20


@pytest.mark.django_db
def test_header_token_authenticates_without_a_cookie():
    get_user_model().objects.create_user(
        username="token-user",
        email="token@example.test",
        password="strong-test-password",
    )
    token = _login(APIClient(enforce_csrf_checks=True))
    # A fresh client holds no cookie: only the header travels.
    header_only = APIClient()
    response = header_only.get("/api/v1/auth/me/", HTTP_X_SESSION_TOKEN=token)
    assert response.status_code == 200
    assert response.json()["username"] == "token-user"


@pytest.mark.django_db
def test_header_token_allows_a_write_without_the_csrf_cookie():
    get_user_model().objects.create_user(
        username="token-user",
        email="token@example.test",
        password="strong-test-password",
    )
    token = _login(APIClient(enforce_csrf_checks=True))
    header_only = APIClient(enforce_csrf_checks=True)
    response = header_only.post(
        "/api/v1/auth/logout/",
        {},
        format="json",
        HTTP_X_SESSION_TOKEN=token,
    )
    # No CSRF cookie is present; a custom-header token cannot be replayed by a
    # cross-site page, so the write is accepted.
    assert response.status_code == 204


@pytest.mark.django_db
def test_an_unknown_token_is_auth_rejected():
    client = APIClient()
    response = client.get(
        "/api/v1/auth/me/",
        HTTP_X_SESSION_TOKEN="not-a-real-session-key",
    )
    assert response.status_code in (401, 403)
    assert response.json()["error"]["code"] in (
        "AUTHENTICATION_REQUIRED",
        "AUTHENTICATION_FAILED",
    )
