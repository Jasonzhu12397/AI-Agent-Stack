"""Process-local cache for resolved agent capabilities, keyed on revision.

The cache holds only successfully resolved active capabilities. Every entry
records the owning installation id, its revision at resolve time, a definition
signature (version, enabled flag, manifest hash) and the resolved capability
payload. The router re-verifies those values against the database before
serving an entry, so an uninstall, disable, revision bump or definition change
in another worker process is rejected on the next resolve instead of waiting
for a TTL to expire.

The cache is intentionally process-local: cross-process invalidation travels
through the installation revision column, not through shared in-memory state.
Entries are kept in a bounded LRU so an unbounded number of projects cannot
grow the cache without limit.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

MAX_CACHE_ENTRIES = 512


def _key(tenant_id: str, project_id: str, capability_name: str) -> tuple[str, str, str]:
    return (str(tenant_id), str(project_id), str(capability_name))


class _CapabilityCache:
    """Thread-safe bounded LRU cache with hit/miss/eviction statistics."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self._max_entries = max_entries
        self._store: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def lookup(
        self, tenant_id: str, project_id: str, capability_name: str
    ) -> dict[str, Any] | None:
        """Return a copy of the cached entry, or ``None`` on a miss."""
        key = _key(tenant_id, project_id, capability_name)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return dict(entry)

    def store(
        self,
        tenant_id: str,
        project_id: str,
        capability_name: str,
        entry: dict[str, Any],
    ) -> None:
        """Store an entry, evicting the least-recently-used entry at the limit."""
        key = _key(tenant_id, project_id, capability_name)
        with self._lock:
            self._store[key] = dict(entry)
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
                self._evictions += 1

    def invalidate(self, installation: Any) -> None:
        """Drop every cached entry bound to the given installation."""
        target = str(installation.pk)
        self._invalidate_installation_id(target)

    def invalidate_installation_id(self, installation_id: Any) -> None:
        """Drop every cached entry bound to the given installation id."""
        self._invalidate_installation_id(str(installation_id))

    def _invalidate_installation_id(self, target: str) -> None:
        with self._lock:
            stale = [
                cache_key
                for cache_key, entry in self._store.items()
                if entry.get("installation_id") == target
            ]
            for cache_key in stale:
                del self._store[cache_key]

    def invalidate_definition(self, definition_id: Any) -> None:
        """Drop every cached entry bound to the given definition id."""
        target = str(definition_id)
        with self._lock:
            stale = [
                cache_key
                for cache_key, entry in self._store.items()
                if entry.get("definition_id") == target
            ]
            for cache_key in stale:
                del self._store[cache_key]

    def clear(self) -> None:
        """Remove all entries and reset the statistics counters."""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def stats(self) -> dict[str, int]:
        """Return cache usage counters: hits, misses, evictions, size."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "size": len(self._store),
            }


_cache = _CapabilityCache()


def lookup(tenant_id: str, project_id: str, capability_name: str) -> dict[str, Any] | None:
    return _cache.lookup(tenant_id, project_id, capability_name)


def store(
    tenant_id: str,
    project_id: str,
    capability_name: str,
    entry: dict[str, Any],
) -> None:
    _cache.store(tenant_id, project_id, capability_name, entry)


def invalidate(installation: Any) -> None:
    _cache.invalidate(installation)


def invalidate_installation_id(installation_id: Any) -> None:
    _cache.invalidate_installation_id(installation_id)


def invalidate_definition(definition_id: Any) -> None:
    _cache.invalidate_definition(definition_id)


def clear() -> None:
    _cache.clear()


def cache_stats() -> dict[str, int]:
    return _cache.stats()
