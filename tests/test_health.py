from django.test import Client


def test_liveness_returns_ok() -> None:
    response = Client().get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_root_exposes_service_identity() -> None:
    response = Client().get("/api/v1/")
    assert response.status_code == 200
    assert response.json() == {"name": "AI Agent Stack API", "version": "0.2.0"}
