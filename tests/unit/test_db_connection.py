"""Tests for database connection setup."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_calendar.db import connect


def test_connect_configures_file_database(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "calendar.db"

    connection = connect(database)

    assert database.parent.is_dir()
    assert connection.row_factory is sqlite3.Row
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    connection.close()


def test_connect_uses_autocommit(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")

    connection.execute("INSERT INTO t (value) VALUES (?)", ("a",))

    # No explicit commit: autocommit means the row is already durable.
    assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    connection.close()


def test_connect_memory_database() -> None:
    connection = connect()

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    connection.close()
