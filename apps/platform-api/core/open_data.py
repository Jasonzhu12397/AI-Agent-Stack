"""Map and weather capabilities served inside the platform.

The console's connector catalog declares map and weather as built-in
capabilities, so they work in any deployment without an operator-supplied
endpoint or API key: place lookup uses the Open-Meteo geocoder, route planning
uses the OSRM demo router, and weather comes from wttr.in.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

GEOCODE_URL = os.getenv("GEOCODE_URL", "https://geocoding-api.open-meteo.com/v1/search")
OSRM_URL = os.getenv("OSRM_URL", "https://router.project-osrm.org")
WEATHER_API_URL = os.getenv("WEATHER_API_URL", "https://wttr.in")
USER_AGENT = os.getenv(
    "CONNECTOR_USER_AGENT",
    "ai-agent-stack-connector/1.0 (+https://github.com/Jasonzhu12397/AI-Agent-Stack)",
)
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("CONNECTOR_UPSTREAM_TIMEOUT", "12"))
MAX_DAYS = 7


class ConnectorError(Exception):
    """An upstream or argument problem the caller should see as an error."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 0.3


def _fetch_json(url: str, params: dict[str, str] | None = None) -> object:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    target = f"{url}?{query}" if query else url
    last_error: Exception | None = None
    # These public services answer 503 for a burst of requests from one
    # datacenter IP; a short retry keeps a demo turn from failing on that.
    for attempt in range(FETCH_ATTEMPTS):
        request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc
        time.sleep(FETCH_BACKOFF_SECONDS * (attempt + 1))
    raise ConnectorError(
        "UPSTREAM_FAILED", f"Upstream request failed: {last_error}", 502
    )


def _text(params: dict[str, list[str]], key: str, *, required: bool = True) -> str:
    values = params.get(key) or []
    value = values[0].strip() if values else ""
    if required and not value:
        raise ConnectorError("INVALID_ARGUMENTS", f"{key} is required.")
    return value


def _float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return None if value is None else int(float(value))
    except (TypeError, ValueError):
        return None


def _place(row: dict, fallback: str) -> dict:
    """A place payload with the keys the declared output schema allows."""

    item = {"name": row.get("name") or fallback}
    for key in ("admin1", "country", "kind"):
        if isinstance(row.get(key), str) and row[key]:
            item[key] = row[key]
    for key in ("latitude", "longitude"):
        number = _float(row.get(key))
        if number is not None:
            item[key] = number
    return item


def _coordinate(params: dict[str, list[str]], key: str) -> float:
    raw = _text(params, key)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConnectorError("INVALID_ARGUMENTS", f"{key} must be a number.") from exc


GEOCODE_CACHE: dict[str, tuple[float, dict]] = {}
GEOCODE_TTL_SECONDS = 600


def _geocode(place: str) -> dict:
    cached = GEOCODE_CACHE.get(place)
    if cached is not None and time.monotonic() - cached[0] < GEOCODE_TTL_SECONDS:
        return cached[1]
    body = _fetch_json(
        GEOCODE_URL, {"name": place, "count": "1", "language": "zh", "format": "json"}
    )
    rows = body.get("results") if isinstance(body, dict) else None
    if not rows:
        raise ConnectorError("PLACE_NOT_FOUND", f"No place matched {place!r}.", 404)
    row = rows[0]
    resolved = _place(
        {
            "name": row.get("name") or place,
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "kind": row.get("feature_code"),
            "admin1": row.get("admin1"),
            "country": row.get("country"),
        },
        place,
    )
    GEOCODE_CACHE[place] = (time.monotonic(), resolved)
    return resolved


def _place_search(params: dict[str, list[str]]) -> dict:
    query = _text(params, "query")
    limit = min(int(_text(params, "limit", required=False) or 5), 10)
    body = _fetch_json(
        GEOCODE_URL, {"name": query, "count": str(limit), "language": "zh", "format": "json"}
    )
    rows = body.get("results") if isinstance(body, dict) else []
    if not rows:
        raise ConnectorError("PLACE_NOT_FOUND", f"No place matched {query!r}.", 404)
    places = [
        _place(
            {
                "name": row.get("name"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "kind": row.get("feature_code"),
                "admin1": row.get("admin1"),
                "country": row.get("country"),
            },
            query,
        )
        for row in rows
    ]
    return {"ok": True, "query": query, "count": len(places), "places": places}


def _route(params: dict[str, list[str]]) -> dict:
    origin = _geocode(_text(params, "origin"))
    destination = _geocode(_text(params, "destination"))
    mode = _text(params, "mode", required=False) or "driving"
    if mode not in ("driving", "walking", "cycling"):
        raise ConnectorError("INVALID_ARGUMENTS", "mode must be driving, walking or cycling.")
    profile = {"driving": "driving", "walking": "walking", "cycling": "cycling"}[mode]
    coordinates = (
        f"{origin['longitude']},{origin['latitude']};"
        f"{destination['longitude']},{destination['latitude']}"
    )
    body = _fetch_json(
        f"{OSRM_URL}/route/v1/{profile}/{coordinates}",
        {"overview": "false", "steps": "false"},
    )
    routes = body.get("routes") if isinstance(body, dict) else None
    if not routes:
        raise ConnectorError("ROUTE_NOT_FOUND", "No route between those places.", 404)
    route = routes[0]
    return {
        "ok": True,
        "mode": mode,
        "origin": origin,
        "destination": destination,
        "distance_km": round(float(route["distance"]) / 1000, 1),
        "duration_minutes": int(round(float(route["duration"]) / 60)),
    }


def _weather_body(place_name: str, latitude: float | None, longitude: float | None) -> dict:
    """Fetch one wttr.in j1 document for a named place or a coordinate pair."""

    if place_name:
        target = urllib.parse.quote(place_name, safe="")
    else:
        if latitude is None or longitude is None:
            raise ConnectorError("INVALID_ARGUMENTS", "Pass place, or latitude and longitude.")
        target = f"{latitude:.4f},{longitude:.4f}"
    body = _fetch_json(f"{WEATHER_API_URL}/{target}", {"format": "j1", "lang": "zh"})
    if not isinstance(body, dict) or not body.get("current_condition"):
        raise ConnectorError("UPSTREAM_FAILED", "Weather response was empty.", 502)
    return body


def _weather(params: dict[str, list[str]], *, forecast: bool) -> dict:
    place_name = _text(params, "place", required=False)
    resolved = None
    if place_name:
        # Resolve the name ourselves: the provider's own lookup picked a
        # neighbouring village for "西安" and reported weather for it.
        resolved = _geocode(place_name)
        latitude, longitude = resolved["latitude"], resolved["longitude"]
    else:
        latitude = _coordinate(params, "latitude")
        longitude = _coordinate(params, "longitude")
    body = _weather_body("" if resolved else place_name, latitude, longitude)
    nearest = body.get("nearest_area") or [{}]
    area = nearest[0] if isinstance(nearest[0], dict) else {}
    place = _place(
        {
            "name": (resolved or {}).get("name")
            or place_name
            or f"{latitude:.3f},{longitude:.3f}",
            "admin1": (area.get("areaName") or [{}])[0].get("value")
            if isinstance(area.get("areaName"), list)
            else None,
            "country": (area.get("country") or [{}])[0].get("value")
            if isinstance(area.get("country"), list)
            else None,
        },
        place_name or "weather",
    )
    if forecast:
        days = min(int(_text(params, "days", required=False) or 3), MAX_DAYS)
        rows = body.get("weather") or []
        return {
            "ok": True,
            "place": place,
            "days": [
                {
                    "date": str(row.get("date") or ""),
                    "temperature_max_c": _float(row.get("maxtempC")),
                    "temperature_min_c": _float(row.get("mintempC")),
                    "description": (
                        ((row.get("hourly") or [{}])[len(row.get("hourly") or [{}]) // 2]
                         .get("weatherDesc") or [{}])[0].get("value")
                    ),
                    "precipitation_probability": max(
                        (
                            _int(hour.get("chanceofrain")) or 0
                            for hour in row.get("hourly") or []
                        ),
                        default=0,
                    ),
                }
                for row in rows[:days]
            ],
        }
    current = body["current_condition"][0]
    payload = {"ok": True, "place": place}
    observed = current.get("localObsDateTime") or current.get("observation_time")
    if isinstance(observed, str) and observed:
        payload["observed_at"] = observed
    for key, source in (
        ("temperature_c", "temp_C"),
        ("apparent_temperature_c", "FeelsLikeC"),
        ("humidity_percent", "humidity"),
        ("wind_kmh", "windspeedKmph"),
    ):
        number = _float(current.get(source))
        if number is not None:
            payload[key] = number
    description = ((current.get("weatherDesc") or [{}])[0]).get("value")
    if isinstance(description, str) and description:
        payload["description"] = description
    return payload


CAPABILITIES = {
    "map.place.search": _place_search,
    "map.route.plan": _route,
    "weather.current": lambda params: _weather(params, forecast=False),
    "weather.forecast": lambda params: _weather(params, forecast=True),
}


def invoke(capability: str, arguments: dict) -> dict:
    """Run one built-in capability; raises ConnectorError on bad input."""

    handler = CAPABILITIES.get(capability)
    if handler is None:
        raise ConnectorError("CAPABILITY_UNKNOWN", f"{capability} is not a built-in capability.")
    params: dict[str, list[str]] = {}
    for key, value in (arguments or {}).items():
        if value is None:
            continue
        if isinstance(value, list):
            params[key] = [str(item) for item in value]
        else:
            params[key] = [str(value)]
    return handler(params)
