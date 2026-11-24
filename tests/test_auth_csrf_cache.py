"""The CSRF token endpoint must never be cached by an intermediary.

Codespaces and other proxies sit between the browser and the console. If they
serve a cached token body while the browser holds a newer cookie, every write
request is rejected with "CSRF token incorrect" and the login page looks broken
even though the API is healthy. These probes pin the cache headers and the
rotation behaviour behind that failure.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

CSRF_URL = "/api/v1/auth/csrf/"
LOGIN_URL = "/api/v1/auth/login/"


@pytest.mark.django_db
def test_csrf_response_is_not_cacheable():
    response = APIClient().get(CSRF_URL)
    assert response.status_code == 200
    assert "csrf_token" in response.json()
    cache_control = response["Cache-Control"]
    assert "no-store" in cache_control
    assert "private" in cache_control
    vary = response["Vary"].lower()
    assert "cookie" in vary


@pytest.mark.django_db
def test_stale_token_is_rejected_and_a_fresh_one_accepted():
    user = get_user_model().objects.create_user(username="csrf-demo", password="pw-12345678")
    client = APIClient()
    stale = client.get(CSRF_URL).json()["csrf_token"]
    # A login rotates the cookie, so the earlier token no longer matches it.
    client.post(
        LOGIN_URL,
        {"username": "csrf-demo", "password": "fluctuating"},
        format="json",
        HTTP_X_CSRFTOKEN=stale,
    )
    fresh = client.get(CSRF_URL).json()["csrf_token"]
    assert fresh != stale
    response = client.post(
        LOGIN_URL,
        {"username": user.username, "password": "pw-12345678"},
        format="json",
        HTTP_X_CSRFTOKEN=fresh,
    )
    assert response.status_code == 200
    assert response.json()["username"] == "csrf-demo"
