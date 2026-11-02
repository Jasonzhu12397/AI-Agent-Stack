from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_liveness_returns_ok() -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_exposes_service_identity() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"
