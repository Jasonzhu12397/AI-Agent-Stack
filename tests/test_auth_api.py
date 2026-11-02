import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_session_login_current_user_and_logout():
    user_model = get_user_model()
    user_model.objects.create_user(
        username="session-user",
        email="session@example.test",
        password="strong-test-password",
    )
    client = APIClient(enforce_csrf_checks=True)
    csrf_response = client.get("/api/v1/auth/csrf/")
    csrf_token = csrf_response.json()["csrf_token"]

    login_response = client.post(
        "/api/v1/auth/login/",
        {"username": "session-user", "password": "strong-test-password"},
        format="json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert login_response.status_code == 200
    assert login_response.json()["username"] == "session-user"

    current_response = client.get("/api/v1/auth/me/")
    assert current_response.status_code == 200
    assert current_response.json()["email"] == "session@example.test"

    refreshed_csrf = client.get("/api/v1/auth/csrf/").json()["csrf_token"]
    logout_response = client.post(
        "/api/v1/auth/logout/",
        {},
        format="json",
        HTTP_X_CSRFTOKEN=refreshed_csrf,
    )
    assert logout_response.status_code == 204
    assert client.get("/api/v1/auth/me/").status_code == 403


@pytest.mark.django_db
def test_login_rejects_invalid_credentials():
    client = APIClient()
    response = client.post(
        "/api/v1/auth/login/",
        {"username": "unknown", "password": "wrong"},
        format="json",
    )
    assert response.status_code == 400
