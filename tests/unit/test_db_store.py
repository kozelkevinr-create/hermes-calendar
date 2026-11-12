"""Tests for store locations and opening helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_calendar.db import store
from hermes_calendar.db.migrations import SCHEMA_VERSION
from hermes_calendar.db.store import (
    GLOBAL_DB_FILENAME,
    PROFILE_DB_FILENAME,
    open_global_store,
    open_profile_store,
    profile_name,
)


def test_open_profile_store_uses_the_active_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "coding"
    monkeypatch.setenv("HERMES_HOME", str(home))

    connection = open_profile_store()

    try:
        path = home / "plugin-data" / "hermes-calendar" / PROFILE_DB_FILENAME
        assert path.is_file()
        assert profile_name() == "coding"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM schema_meta")
        }
        assert meta["store_scope"] == "profile"
        assert meta["store_id"] == "profile:coding"
        assert meta["owner_profile_id"] == "coding"
    finally:
        connection.close()


def test_open_global_store_uses_the_shared_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "coding"
    monkeypatch.setenv("HERMES_HOME", str(home))

    connection = open_global_store()

    try:
        path = tmp_path / "root" / "plugin-data" / "hermes-calendar" / GLOBAL_DB_FILENAME
        assert path.is_file()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM schema_meta")
        }
        assert meta["store_scope"] == "global"
        assert meta["store_id"] == "global"
    finally:
        connection.close()


def test_open_store_closes_the_connection_when_migration_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "coding"
    monkeypatch.setenv("HERMES_HOME", str(home))
    opened: list[sqlite3.Connection] = []
    real_open_db = store.open_db

    def recording_open_db(path: Path) -> sqlite3.Connection:
        connection = real_open_db(path)
        opened.append(connection)
        return connection

    def failing_migrate(*args: object, **kwargs: object) -> int:
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(store, "open_db", recording_open_db)
    monkeypatch.setattr(store, "migrate", failing_migrate)

    with pytest.raises(RuntimeError, match="migration exploded"):
        open_profile_store()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_open_store_retries_a_locked_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "coding"
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_open_db = store.open_db
    calls = 0

    def flaky_open_db(path: Path) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_open_db(path)

    monkeypatch.setattr(store, "open_db", flaky_open_db)
    monkeypatch.setattr(store, "_OPEN_RETRY_DELAY_SECONDS", 0)

    connection = open_profile_store()

    try:
        assert calls == 3
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        connection.close()


def test_open_store_does_not_retry_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "coding"))
    calls = 0

    def failing_open_db(path: Path) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "open_db", failing_open_db)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        open_profile_store()

    assert calls == 1


def test_open_store_gives_up_after_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "coding"))
    calls = 0

    def locked_open_db(path: Path) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is busy")

    monkeypatch.setattr(store, "open_db", locked_open_db)
    monkeypatch.setattr(store, "_OPEN_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(sqlite3.OperationalError, match="busy"):
        open_profile_store()

    assert calls == store._OPEN_RETRIES
