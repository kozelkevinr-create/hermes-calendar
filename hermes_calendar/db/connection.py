"""Connection setup for the calendar database.

Design constraints (``docs/design/core.md`` §4.1):

* WAL journaling, exactly one writer, concurrent readers.
* Foreign keys enforced by SQLite, not only by application code.
* A busy timeout so a reader waits instead of raising "database is locked".
* Connections are left in autocommit mode so callers can open explicit
  ``BEGIN IMMEDIATE`` transactions (the single-writer claim path).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_SYNCHRONOUS = "NORMAL"


def connect(
    database: str | Path = ":memory:",
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a configured connection to ``database``.

    Args:
        database: Filesystem path or ``":memory:"``. Parent directories are
            created for file-backed databases.
        busy_timeout_ms: Milliseconds a blocked statement waits for the write
            lock before raising ``sqlite3.OperationalError``.

    Returns:
        A connection with ``row_factory = sqlite3.Row``, foreign keys on, WAL
        journaling (file-backed databases only), and autocommit isolation.
    """
    target = str(database)
    if target != ":memory:":
        Path(target).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(target, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = NORMAL")
    if target != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    return connection
