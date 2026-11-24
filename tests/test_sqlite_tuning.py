"""The development console runs API and worker on one SQLite file."""

from __future__ import annotations

import pytest
from core import sqlite_tuning
from django.conf import settings
from django.db import connection


def test_sqlite_detection_matches_the_configured_engine():
    if "sqlite" in str(settings.DATABASES["default"].get("ENGINE", "")):
        assert sqlite_tuning.is_sqlite(settings.DATABASES) is True
    else:
        assert sqlite_tuning.is_sqlite(settings.DATABASES) is False


@pytest.mark.django_db
def test_sqlite_connection_sets_a_busy_timeout_and_wal_on_a_file_database():
    if connection.vendor != "sqlite":
        pytest.skip("not running on SQLite")
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout;")
        busy_timeout = cursor.fetchone()[0]
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        cursor.execute("PRAGMA database_list;")
        databases = cursor.fetchall()
    # The timeout is what stops "database is locked"; an in-memory test database
    # cannot enter WAL, so the journal mode is only asserted for a real file.
    assert busy_timeout == sqlite_tuning.BUSY_TIMEOUT_MS
    file_backed = any(row[2] not in ("", ":memory:") for row in databases)
    if file_backed:
        assert journal_mode.lower() == "wal"
