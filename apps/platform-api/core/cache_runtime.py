"""Cache and coordination helpers built on the Django cache framework.

The cache is optional by design. When ``CACHE_ENABLED`` is off the project uses
the local in-memory backend and every helper below degrades to a no-op, so a
missing Redis instance never turns a request into a 500. When the cache is
enabled and Redis becomes unreachable, the helpers log a warning and fail open
(or closed, for lock acquisition with ``fail_open=False``) instead of raising.

Redis must never be the only persistence source: these helpers only hold
derived data (lookup results, health probes, single-flight locks), and losing
the whole cache can only cost a recomputation or a repeated attempt.
"""

import json
import logging
import uuid

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

HEALTH_KEY = "system:cache-health"
DEFAULT_JSON_TIMEOUT = 300
DEFAULT_LOCK_TIMEOUT = 60
DEFAULT_HEALTH_TIMEOUT = 10


def cache_enabled():
    return bool(getattr(settings, "CACHE_ENABLED", False))


def _timeout(value, fallback):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _json_timeout():
    configured = getattr(settings, "CACHE_DEFAULT_TIMEOUT", DEFAULT_JSON_TIMEOUT)
    return _timeout(configured, DEFAULT_JSON_TIMEOUT)


def _resolve_timeout(timeout, fallback_setting, fallback):
    if timeout is None:
        return _timeout(getattr(settings, fallback_setting, fallback), fallback)
    return _timeout(timeout, fallback)


def cache_health():
    """Return ``disabled``, ``ok``, or ``unavailable`` for readiness reporting."""

    if not cache_enabled():
        return "disabled"
    token = uuid.uuid4().hex
    timeout = _timeout(
        getattr(settings, "CACHE_HEALTH_TIMEOUT", DEFAULT_HEALTH_TIMEOUT),
        DEFAULT_HEALTH_TIMEOUT,
    )
    try:
        cache.set(HEALTH_KEY, token, timeout=timeout)
        return "ok" if cache.get(HEALTH_KEY) == token else "unavailable"
    except Exception as exc:  # noqa: BLE001 - health probes must not raise
        logger.warning("Cache health probe failed: %s", exc)
        return "unavailable"


def get_json(key, default=None):
    """Read a JSON value, returning ``default`` when absent or unavailable."""

    if not cache_enabled():
        return default
    try:
        raw = cache.get(key)
    except Exception as exc:  # noqa: BLE001 - fail open on cache outages
        logger.warning("Cache read failed for %s: %s", key, exc)
        return default
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Cache value for %s is not valid JSON; ignoring it.", key)
        return default


def set_json(key, value, timeout=None):
    """Store a JSON value; returns ``False`` when the cache is off or failing."""

    if not cache_enabled():
        return False
    effective_timeout = _resolve_timeout(timeout, "CACHE_DEFAULT_TIMEOUT", DEFAULT_JSON_TIMEOUT)
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        cache.set(key, payload, timeout=effective_timeout)
        return True
    except Exception as exc:  # noqa: BLE001 - fail open on cache outages
        logger.warning("Cache write failed for %s: %s", key, exc)
        return False


def delete(key):
    if not cache_enabled():
        return False
    try:
        cache.delete(key)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache delete failed for %s: %s", key, exc)
        return False


def increment(key, delta=1, timeout=None):
    """Atomically increment a counter; returns ``None`` when unavailable."""

    if not cache_enabled():
        return None
    effective_timeout = _resolve_timeout(timeout, "CACHE_DEFAULT_TIMEOUT", DEFAULT_JSON_TIMEOUT)
    try:
        try:
            return cache.incr(key, delta)
        except ValueError:
            cache.add(key, delta, timeout=effective_timeout)
            return delta
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache increment failed for %s: %s", key, exc)
        return None


def acquire_lock(key, timeout_seconds=None, fail_open=False):
    """Acquire a single-flight lock and return its token, or ``None``.

    With ``fail_open=True`` an unreachable cache yields a token anyway, which
    trades strict mutual exclusion for availability; the caller then runs the
    protected work without a real lock. ``release_lock`` is safe either way.
    """

    if not cache_enabled():
        return None
    timeout = _resolve_timeout(
        timeout_seconds,
        "CACHE_LOCK_TIMEOUT",
        DEFAULT_LOCK_TIMEOUT,
    )
    token = uuid.uuid4().hex
    try:
        acquired = cache.add(key, token, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache lock acquisition failed for %s: %s", key, exc)
        return token if fail_open else None
    return token if acquired else None


def release_lock(key, token):
    """Release a lock only when this caller still owns it."""

    if not cache_enabled() or not token:
        return False
    try:
        if cache.get(key) == token:
            cache.delete(key)
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache lock release failed for %s: %s", key, exc)
        return False
