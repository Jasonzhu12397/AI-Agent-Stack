"""SQLite tuning for the single-host development console.

The console runs one daphne process plus one worker process against the same
SQLite file. Both write: the worker advances ``ScheduledChatJob.next_run_at``
every thirty seconds, and the API persists chat turns. SQLite's default journal
mode locks the whole database for every write, so a turn that lands while the
worker is claiming a job fails with "database is locked".

WAL lets readers and a single writer overlap, and ``busy_timeout`` makes a
contender wait for the write lock instead of failing immediately. This is
development-only: production uses PostgreSQL, where the whole problem does not
exist. ``DATABASES`` therefore only applies these settings when the engine is
SQLite.
"""

from __future__ import annotations

import sqlite3

from django.db.backends.signals import connection_created
from django.dispatch import receiver

BUSY_TIMEOUT_MS = 20_000


def is_sqlite(settings_databases: dict) -> bool:
    config = settings_databases.get("default", {})
    return "sqlite" in str(config.get("ENGINE", ""))


@receiver(connection_created)
def tune_sqlite_connection(sender, connection, **kwargs):  # noqa: ANN001 - Django signal
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
        cursor.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.Error:
        # A read-only or network file system cannot switch to WAL; the console
        # still works, it just keeps the default rollback journal.
        return
    finally:
        cursor.close()
