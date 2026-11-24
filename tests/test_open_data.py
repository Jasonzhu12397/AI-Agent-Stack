"""Unit tests for the open-data connector's capability payloads."""

import pytest
from core import open_data as connector


def _geocode_payload():
    return {
        "results": [
            {
                "name": "西安",
                "latitude": 34.25833,
                "longitude": 108.92861,
                "feature_code": "PPLA",
                "admin1": "陕西",
                "country": "中国",
            }
        ]
    }


def _weather_payload():
    return {
        "current_condition": [
            {
                "temp_C": "5",
                "FeelsLikeC": "3",
                "humidity": "66",
                "windspeedKmph": "4",
                "observation_time": "03:15 PM",
                "weatherDesc": [{"value": "Smoky haze"}],
            }
        ],
        "nearest_area": [
            {"areaName": [{"value": "西安"}], "country": [{"value": "China"}]}
        ],
        "weather": [
            {
                "date": "2026-11-25",
                "maxtempC": "15",
                "mintempC": "3",
                "hourly": [
                    {"chanceofrain": "10", "weatherDesc": [{"value": "Haze"}]},
                    {"chanceofrain": "21", "weatherDesc": [{"value": "Haze"}]},
                ],
            }
        ],
    }


def test_place_search_maps_geocoder_rows(monkeypatch):
    calls = {}

    def fake_fetch(url, params=None):
        calls["params"] = params
        return _geocode_payload()

    monkeypatch.setattr(connector, "_fetch_json", fake_fetch)
    payload = connector.CAPABILITIES["map.place.search"]({"query": ["西安"], "limit": ["2"]})
    assert payload["count"] == 1
    assert payload["places"][0]["name"] == "西安"
    assert payload["places"][0]["latitude"] == pytest.approx(34.25833)
    assert calls["params"]["count"] == "2"


def test_route_plan_geocodes_both_places(monkeypatch):
    def fake_fetch(url, params=None):
        if "route" in url:
            return {"code": "Ok", "routes": [{"distance": 14422.7, "duration": 951}]}
        return _geocode_payload()

    monkeypatch.setattr(connector, "_fetch_json", fake_fetch)
    payload = connector.CAPABILITIES["map.route.plan"](
        {"origin": ["西安"], "destination": ["咸阳"]}
    )
    assert payload["distance_km"] == pytest.approx(14.4)
    assert payload["duration_minutes"] == 16


def test_weather_current_geocodes_instead_of_trusting_the_provider(monkeypatch):
    seen = {}

    def fake_fetch(url, params=None):
        if "geocoding" in url or "search" in url:
            return _geocode_payload()
        seen["url"] = url
        return _weather_payload()

    monkeypatch.setattr(connector, "_fetch_json", fake_fetch)
    payload = connector.CAPABILITIES["weather.current"]({"place": ["西安"]})
    assert payload["temperature_c"] == 5.0
    assert payload["place"]["name"] == "西安"
    assert "34.2583" in seen["url"]


def test_weather_forecast_returns_days(monkeypatch):
    def fake_fetch(url, params=None):
        return _geocode_payload() if "search" in url else _weather_payload()

    monkeypatch.setattr(connector, "_fetch_json", fake_fetch)
    payload = connector.CAPABILITIES["weather.forecast"]({"place": ["西安"], "days": ["1"]})
    assert len(payload["days"]) == 1
    assert payload["days"][0]["precipitation_probability"] == 21


def test_unknown_place_is_reported(monkeypatch):
    monkeypatch.setattr(connector, "_fetch_json", lambda url, params=None: {"results": []})
    with pytest.raises(connector.ConnectorError) as excinfo:
        connector.CAPABILITIES["map.place.search"]({"query": ["zzzz"]})
    assert excinfo.value.code == "PLACE_NOT_FOUND"


def test_place_inside_a_sentence_still_resolves(monkeypatch):
    def fake_fetch(url, params=None):
        if "search" in url:
            return _geocode_payload() if params.get("name") == "西安" else {"results": []}
        return _weather_payload()

    monkeypatch.setattr(connector, "_fetch_json", fake_fetch)
    payload = connector.CAPABILITIES["weather.current"]({"place": ["帮我查一下西安天气"]})
    assert payload["place"]["name"] == "西安"
