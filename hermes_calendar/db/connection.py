"""Connection setup for the calendar database.

Two databases are in play, both configured by the shared plugin storage helper
so the pragma stack is defined once:

* the per-profile database: one writer; concurrent readers.
* the global database: several writer processes, serialized by SQLite WAL +
  ``BEGIN IMMEDIATE``.

Foreign keys are enforced by SQLite, not only by application code. Connections
are left in autocommit mode so callers can open explicit transactions
(``BEGIN IMMEDIATE`` for writes), and a busy timeout makes contention wait
instead of raising "database is locked".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..plugin_storage import open_db


def connect(database: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a configured connection to ``database``.

    Args:
        database: Filesystem path or ``":memory:"``. Parent directories are
            created for file-backed databases.

    Returns:
        A connection with ``row_factory = sqlite3.Row``, foreign keys on, WAL
        journaling (file-backed databases only), and autocommit isolation.
    """
    return open_db(Path(database))
