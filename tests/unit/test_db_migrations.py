"""Schema migration and store-invariant tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from hermes_calendar.db import connect, migrations
from hermes_calendar.db.errors import SchemaVersionError, StoreScopeError
from hermes_calendar.db.migrations import (
    GLOBAL_CALENDAR_ID,
    LOCAL_SOURCE_ID,
    SCHEMA_VERSION,
    StoreScope,
    migrate,
)


def _meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM schema_meta")
    }


def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def test_migrate_profile_store_creates_v1_schema(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")

    version = migrate(
        connection,
        store_scope="profile",
        store_id="profile:coding",
        owner_profile_id="coding",
    )

    try:
        assert version == SCHEMA_VERSION
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _meta(connection) == {
            "schema_version": str(SCHEMA_VERSION),
            "created_by": "hermes-calendar",
            "min_available_seq": "1",
            "store_scope": "profile",
            "store_id": "profile:coding",
            "owner_profile_id": "coding",
        }
        tables = _names(connection, "table")
        assert {
            "schema_meta",
            "sources",
            "calendars",
            "events",
            "event_attendees",
            "event_reminders",
            "change_context",
            "change_log",
        } <= tables
        assert {"event_view", "pending_push"} <= _names(connection, "view")
        assert connection.execute("SELECT count(*) FROM calendars").fetchone()[0] == 0
        source = connection.execute("SELECT * FROM sources").fetchone()
        assert source is not None
        assert source["id"] == LOCAL_SOURCE_ID
        assert source["scope"] == "profile"
        assert source["owner_profile_id"] == "coding"
        # Migration seeding is suppressed, so the log starts empty.
        assert connection.execute("SELECT count(*) FROM change_log").fetchone()[0] == 0
    finally:
        connection.close()


def test_migrate_global_store_seeds_exactly_one_global_calendar(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar-global.db")

    migrate(
        connection,
        store_scope="global",
        store_id="global",
        owner_profile_id=None,
    )

    try:
        assert _meta(connection)["owner_profile_id"] == ""
        calendars = connection.execute("SELECT * FROM calendars").fetchall()
        assert len(calendars) == 1
        calendar = calendars[0]
        assert calendar["id"] == GLOBAL_CALENDAR_ID
        assert calendar["scope"] == "global"
        assert calendar["owner_profile_id"] is None

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO calendars
                  (id, source_id, scope, name, default_tz, created_utc, updated_utc)
                VALUES ('cal_second', ?, 'global', 'Second', 'UTC', 0, 0)
                """,
                (LOCAL_SOURCE_ID,),
            )
    finally:
        connection.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    try:
        first = migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )
        second = migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )

        assert first == second == SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
    finally:
        connection.close()


def test_migrate_refuses_a_newer_schema(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    connection.execute("PRAGMA user_version = 99")
    try:
        with pytest.raises(SchemaVersionError, match="newer"):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:one",
                owner_profile_id="one",
            )
    finally:
        connection.close()


def test_migrate_refuses_the_other_store_scope(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    try:
        migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )

        with pytest.raises(StoreScopeError, match="does not match"):
            migrate(
                connection,
                store_scope="global",
                store_id="global",
                owner_profile_id="",
            )
    finally:
        connection.close()


def test_migrate_treats_missing_scope_metadata_as_a_mismatch(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("PRAGMA user_version = 1")
    try:
        with pytest.raises(StoreScopeError, match="None"):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:one",
                owner_profile_id="one",
            )
    finally:
        connection.close()


def test_migrate_validates_scope_and_owner(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    try:
        with pytest.raises(ValueError, match="owner_profile_id"):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:one",
                owner_profile_id=None,
            )
        with pytest.raises(ValueError, match="unknown store scope"):
            migrate(
                connection,
                store_scope=cast(StoreScope, "bogus"),
                store_id="bogus",
                owner_profile_id="one",
            )
    finally:
        connection.close()


def test_migrate_rolls_back_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    monkeypatch.setattr(
        migrations,
        "V1_STATEMENTS",
        ("CREATE TABLE half (id INTEGER)", "this is not sql"),
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:one",
                owner_profile_id="one",
            )

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "half" not in _names(connection, "table")
    finally:
        connection.close()


def test_scope_guards_reject_cross_scope_rows(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar.db")
    migrate(
        connection,
        store_scope="profile",
        store_id="profile:one",
        owner_profile_id="one",
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="source scope"):
            connection.execute(
                """
                INSERT INTO sources
                  (id, scope, kind, display_name, created_utc, updated_utc)
                VALUES ('src_global', 'global', 'local', 'Bad', 0, 0)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="calendar scope"):
            connection.execute(
                """
                INSERT INTO calendars
                  (id, source_id, scope, name, default_tz, created_utc, updated_utc)
                VALUES ('cal_global', ?, 'global', 'Bad', 'UTC', 0, 0)
                """,
                (LOCAL_SOURCE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="source scope is immutable"):
            connection.execute(
                "UPDATE sources SET scope = 'global' WHERE id = ?", (LOCAL_SOURCE_ID,)
            )

        connection.execute(
            """
            INSERT INTO calendars
              (id, source_id, scope, owner_profile_id, name, default_tz,
               created_utc, updated_utc)
            VALUES ('cal_work', ?, 'profile', 'one', 'Work', 'UTC', 0, 0)
            """,
            (LOCAL_SOURCE_ID,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="calendar scope is immutable"):
            connection.execute("UPDATE calendars SET scope = 'global' WHERE id = 'cal_work'")
    finally:
        connection.close()


def test_global_store_rejects_a_profile_calendar(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar-global.db")
    migrate(
        connection,
        store_scope="global",
        store_id="global",
        owner_profile_id="",
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="calendar scope"):
            connection.execute(
                """
                INSERT INTO calendars
                  (id, source_id, scope, name, default_tz, created_utc, updated_utc)
                VALUES ('cal_private', ?, 'profile', 'Private', 'UTC', 0, 0)
                """,
                (LOCAL_SOURCE_ID,),
            )
    finally:
        connection.close()


def test_migration_skips_v2_when_another_process_won_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = connect(tmp_path / "calendar.db")
    try:
        migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )
        versions = iter((1, SCHEMA_VERSION, SCHEMA_VERSION))
        monkeypatch.setattr(migrations, "_read_user_version", lambda _connection: next(versions))

        version = migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )

        assert version == SCHEMA_VERSION
        assert "reminder_instances" in _names(connection, "table")
    finally:
        connection.close()
