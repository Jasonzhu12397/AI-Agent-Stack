import json

import pytest
from core.cache_runtime import (
    acquire_lock,
    cache_health,
    delete,
    get_json,
    increment,
    release_lock,
    set_json,
)
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _broken_cache(monkeypatch):
    def raise_error(*args, **kwargs):
        raise ConnectionError("redis is unreachable")

    monkeypatch.setattr(cache, "get", raise_error)
    monkeypatch.setattr(cache, "set", raise_error)
    monkeypatch.setattr(cache, "add", raise_error)
    monkeypatch.setattr(cache, "incr", raise_error)
    monkeypatch.setattr(cache, "delete", raise_error)


def test_helpers_are_no_ops_when_the_cache_is_disabled():
    assert cache_health() == "disabled"
    assert get_json("missing", default={"fallback": True}) == {"fallback": True}
    assert set_json("key", {"value": 1}) is False
    assert delete("key") is False
    assert increment("counter") is None
    assert acquire_lock("lock") is None


@override_settings(CACHE_ENABLED=True)
def test_json_roundtrip_and_counters_on_an_available_cache():
    assert cache_health() == "ok"
    assert set_json("lookup", {"items": [1, 2]}) is True
    assert get_json("lookup") == {"items": [1, 2]}
    assert get_json("lookup") == json.loads(json.dumps({"items": [1, 2]}))
    assert delete("lookup") is True
    assert get_json("lookup") is None

    assert increment("attempts") == 1
    assert increment("attempts") == 2
    assert increment("attempts", delta=3) == 5


@override_settings(CACHE_ENABLED=True)
def test_single_flight_lock_only_allows_one_holder():
    first = acquire_lock("ingest-lock")
    assert first
    assert acquire_lock("ingest-lock") is None
    assert release_lock("ingest-lock", first) is True
    assert release_lock("ingest-lock", "other-token") is False
    assert acquire_lock("ingest-lock")


@override_settings(CACHE_ENABLED=True)
def test_unreachable_cache_fails_open_and_reports_unavailable(monkeypatch):
    _broken_cache(monkeypatch)

    assert cache_health() == "unavailable"
    assert get_json("lookup", default="fallback") == "fallback"
    assert set_json("lookup", {"value": 1}) is False
    assert delete("lookup") is False
    assert increment("attempts") is None
    assert acquire_lock("ingest-lock", fail_open=True)
    assert acquire_lock("ingest-lock", fail_open=False) is None
    assert release_lock("ingest-lock", "token") is False


@pytest.mark.django_db
@override_settings(CACHE_ENABLED=False)
def test_readiness_reports_a_disabled_cache(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    response = APIClient().get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "vector_store": "not_configured",
        "cache": "disabled",
    }


@pytest.mark.django_db
@override_settings(CACHE_ENABLED=True)
def test_readiness_reports_a_healthy_cache(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    response = APIClient().get("/health/ready")
    assert response.status_code == 200
    assert response.json()["cache"] == "ok"


@pytest.mark.django_db
@override_settings(CACHE_ENABLED=True, CACHE_REQUIRED=True)
def test_readiness_fails_when_a_required_cache_is_unavailable(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setattr("core.views.cache_health", lambda: "unavailable")
    response = APIClient().get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "ok",
        "vector_store": "not_configured",
        "cache": "unavailable",
    }


@pytest.mark.django_db
@override_settings(CACHE_ENABLED=True, CACHE_REQUIRED=False)
def test_readiness_stays_ready_when_an_optional_cache_is_unavailable(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setattr("core.views.cache_health", lambda: "unavailable")
    response = APIClient().get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "vector_store": "not_configured",
        "cache": "unavailable",
    }
