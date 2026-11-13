"""Schema migration and store-invariant tests."""

from __future__ import annotations

import sqlite3
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from hermes_calendar.db import connect, migrations
from hermes_calendar.db.errors import MigrationError, SchemaVersionError, StoreScopeError
from hermes_calendar.db.migrations import (
    DEFAULT_PROFILE_CALENDAR_NAME,
    GLOBAL_CALENDAR_ID,
    LOCAL_SOURCE_ID,
    SCHEMA_VERSION,
    StoreScope,
    migrate,
)
from hermes_calendar.db.schema import V1_STATEMENTS, V2_PROFILE_STATEMENTS
from hermes_calendar.service import CalendarService


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


def _v2_profile_store(connection: sqlite3.Connection, *, owner: str = "one") -> None:
    """Build a schema-v2 profile store without running the v3 data migration."""
    for statement in V1_STATEMENTS:
        connection.execute(statement)
    connection.executemany(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        (
            ("schema_version", "2"),
            ("created_by", "hermes-calendar"),
            ("min_available_seq", "1"),
            ("store_scope", "profile"),
            ("store_id", f"profile:{owner}"),
            ("owner_profile_id", owner),
        ),
    )
    connection.execute("UPDATE change_context SET suppress = 1 WHERE id = 1")
    connection.execute(
        """
        INSERT INTO sources
          (id, scope, owner_profile_id, kind, display_name,
           config_json, capabilities_json, created_utc, updated_utc)
        VALUES (?, 'profile', ?, 'local', 'Local', '{}', '{}', 0, 0)
        """,
        (LOCAL_SOURCE_ID, owner),
    )
    connection.execute("UPDATE change_context SET suppress = 0 WHERE id = 1")
    for statement in V2_PROFILE_STATEMENTS:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 2")


def _calendars(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM calendars ORDER BY name, id").fetchall()


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
        sources = connection.execute("SELECT * FROM sources").fetchall()
        assert len(sources) == 1
        source = sources[0]
        assert source["id"] == LOCAL_SOURCE_ID
        assert source["scope"] == "profile"
        assert source["owner_profile_id"] == "coding"
        # V3 seeds exactly one default calendar for a fresh profile store.
        calendars = _calendars(connection)
        assert len(calendars) == 1
        assert calendars[0]["name"] == DEFAULT_PROFILE_CALENDAR_NAME
        assert calendars[0]["id"].startswith("cal_")
        # The seed goes through change capture as a system write, so it is
        # logged for local refresh handling but never offered for remote push.
        logs = connection.execute("SELECT * FROM change_log").fetchall()
        assert len(logs) == 1
        assert logs[0]["entity_type"] == "calendar"
        assert logs[0]["entity_id"] == calendars[0]["id"]
        assert logs[0]["origin"] == "system"
        assert logs[0]["pushed_utc"] is not None
        assert connection.execute("SELECT count(*) FROM pending_push").fetchone()[0] == 0
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


def test_global_store_does_not_get_a_default_profile_calendar(tmp_path: Path) -> None:
    connection = connect(tmp_path / "calendar-global.db")
    migrate(connection, store_scope="global", store_id="global", owner_profile_id="")
    try:
        names = [str(row["name"]) for row in _calendars(connection)]
        assert names == ["Global"]
        assert connection.execute("SELECT count(*) FROM pending_push").fetchone()[0] == 0
    finally:
        connection.close()


def test_v2_profile_store_with_no_calendars_gains_one_default(tmp_path: Path) -> None:
    connection = connect(tmp_path / "v2.db")
    try:
        _v2_profile_store(connection)
        assert _calendars(connection) == []

        version = migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )

        assert version == SCHEMA_VERSION
        calendars = _calendars(connection)
        assert len(calendars) == 1
        calendar = calendars[0]
        assert calendar["name"] == DEFAULT_PROFILE_CALENDAR_NAME
        assert calendar["source_id"] == LOCAL_SOURCE_ID
        assert calendar["scope"] == "profile"
        assert calendar["owner_profile_id"] == "one"
        assert calendar["default_tz"] == "UTC"
        assert calendar["read_only"] == 0
        assert calendar["deleted_utc"] is None
        assert _meta(connection)["schema_version"] == str(SCHEMA_VERSION)
        log = connection.execute("SELECT * FROM change_log").fetchone()
        assert log is not None
        assert log["origin"] == "system"
        assert connection.execute("SELECT count(*) FROM pending_push").fetchone()[0] == 0
    finally:
        connection.close()


def test_v2_profile_store_with_an_existing_calendar_gains_nothing(tmp_path: Path) -> None:
    connection = connect(tmp_path / "v2.db")
    try:
        _v2_profile_store(connection)
        connection.execute(
            """
            INSERT INTO calendars
              (id, source_id, scope, owner_profile_id, name, default_tz,
               created_utc, updated_utc)
            VALUES ('cal_work', ?, 'profile', 'one', 'Work', 'UTC', 0, 0)
            """,
            (LOCAL_SOURCE_ID,),
        )

        migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )

        assert [str(row["name"]) for row in _calendars(connection)] == ["Work"]
    finally:
        connection.close()


def test_v2_profile_store_with_only_a_deleted_calendar_gains_nothing(tmp_path: Path) -> None:
    connection = connect(tmp_path / "v2.db")
    try:
        _v2_profile_store(connection)
        connection.execute(
            """
            INSERT INTO calendars
              (id, source_id, scope, owner_profile_id, name, default_tz,
               read_only, created_utc, updated_utc, deleted_utc)
            VALUES ('cal_gone', ?, 'profile', 'one', 'Gone', 'UTC', 0, 0, 0, 10)
            """,
            (LOCAL_SOURCE_ID,),
        )

        migrate(
            connection,
            store_scope="profile",
            store_id="profile:one",
            owner_profile_id="one",
        )

        rows = connection.execute("SELECT * FROM calendars").fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "cal_gone"
        assert rows[0]["deleted_utc"] == 10
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
        assert connection.execute("SELECT count(*) FROM calendars").fetchone()[0] == 1
    finally:
        connection.close()


def test_concurrent_first_opens_seed_exactly_one_calendar(tmp_path: Path) -> None:
    path = tmp_path / "racing.db"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def open_once() -> None:
        connection = connect(path)
        try:
            barrier.wait(timeout=5)
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:one",
                owner_profile_id="one",
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=open_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    connection = connect(path)
    try:
        assert [str(row["name"]) for row in _calendars(connection)] == [
            DEFAULT_PROFILE_CALENDAR_NAME
        ]
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
        assert connection.execute("SELECT count(*) FROM calendars").fetchone()[0] == 1
    finally:
        connection.close()


def test_seeded_calendar_supports_events_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "one"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    service = CalendarService()

    listed = service.list_calendars()
    assert listed["count"] == 1
    calendar = listed["calendars"][0]
    assert calendar["name"] == DEFAULT_PROFILE_CALENDAR_NAME

    created = service.create_event(
        calendar_id=calendar["id"],
        summary="First event",
        start="2026-03-02T09:00:00Z",
        end="2026-03-02T10:00:00Z",
        timezone="UTC",
    )
    assert created["event"]["calendar_id"] == calendar["id"]

    events = service.list_events(
        calendar_id=calendar["id"],
        start="2026-03-01T00:00:00Z",
        end="2026-03-03T00:00:00Z",
        timezone="UTC",
    )
    assert events["count"] == 1
    assert events["events"][0]["summary"] == "First event"
    assert events["events"][0]["master_id"] == created["event"]["id"]


# --------------------------------------------------------------- v4 repair


def _v3_profile_store(
    path: Path, *, owner: str = ".hermes", legacy_actors: bool = True
) -> sqlite3.Connection:
    """Build a committed schema-v3 profile store with realistic row counts."""
    connection = connect(path)
    for statement in V1_STATEMENTS:
        connection.execute(statement)
    connection.execute("UPDATE change_context SET suppress = 1 WHERE id = 1")
    connection.executemany(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        (
            ("schema_version", "3"),
            ("created_by", "hermes-calendar"),
            ("min_available_seq", "1"),
            ("store_scope", "profile"),
            ("store_id", f"profile:{owner}"),
            ("owner_profile_id", owner),
        ),
    )
    connection.execute(
        """
        INSERT INTO sources
          (id, scope, owner_profile_id, kind, display_name,
           config_json, capabilities_json, created_utc, updated_utc)
        VALUES (?, 'profile', ?, 'local', 'Local', '{}', '{}', 0, 0)
        """,
        (LOCAL_SOURCE_ID, owner),
    )
    connection.executemany(
        """
        INSERT INTO calendars
          (id, source_id, scope, owner_profile_id, name, default_tz,
           read_only, created_utc, updated_utc)
        VALUES (?, ?, 'profile', ?, ?, 'UTC', 0, 0, 0)
        """,
        (
            ("cal_one", LOCAL_SOURCE_ID, owner, "One"),
            ("cal_two", LOCAL_SOURCE_ID, owner, "Two"),
        ),
    )
    connection.executemany(
        """
        INSERT INTO events
          (id, calendar_id, source_id, scope, owner_profile_id, uid, summary,
           start_local, start_tz, start_utc, end_local, end_tz, end_utc,
           duration_seconds, updated_utc)
        VALUES (?, ?, ?, 'profile', ?, ?, ?, '2026-01-01T09:00:00', 'UTC', 0,
                '2026-01-01T10:00:00', 'UTC', 3600, 3600, 0)
        """,
        (
            (f"evt_{index}", "cal_one", LOCAL_SOURCE_ID, owner, f"uid-{index}", f"Event {index}")
            for index in range(53)
        ),
    )
    for statement in V2_PROFILE_STATEMENTS:
        connection.execute(statement)
    change_actor = owner if legacy_actors else "coding"
    connection.executemany(
        """
        INSERT INTO change_log
          (at_utc, scope, entity_type, entity_id, op, origin, actor, after_json)
        VALUES (0, 'profile', 'event', ?, 'insert', 'local', ?, '{}')
        """,
        ((f"log-{index}", change_actor if index < 53 else "other") for index in range(55)),
    )
    connection.execute(
        """
        UPDATE change_context
           SET origin = 'system', actor = ?, batch_id = 'batch-v3', suppress = 0
         WHERE id = 1
        """,
        (owner,),
    )
    connection.execute("PRAGMA user_version = 3")
    return connection


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.pre-v4-*.bak"))


def _legacy_count(connection: sqlite3.Connection, table: str, column: str) -> int:
    row = connection.execute(
        f'SELECT count(*) FROM "{table}" WHERE "{column}" = ?', (".hermes",)
    ).fetchone()
    return int(row[0])


def test_v4_repairs_every_legacy_profile_id(tmp_path: Path) -> None:
    path = tmp_path / "calendar.db"
    connection = _v3_profile_store(path)
    try:
        before = _table_counts(connection)
        pending_before = int(connection.execute("SELECT count(*) FROM pending_push").fetchone()[0])
        logs_before = int(connection.execute("SELECT count(*) FROM change_log").fetchone()[0])
        assert logs_before == 55
        assert pending_before == 55

        version = migrate(
            connection,
            store_scope="profile",
            store_id="profile:default",
            owner_profile_id="default",
        )

        assert version == SCHEMA_VERSION == 4
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _table_counts(connection) == before
        assert int(connection.execute("SELECT count(*) FROM change_log").fetchone()[0]) == 55
        assert int(connection.execute("SELECT count(*) FROM pending_push").fetchone()[0]) == (
            pending_before
        )

        meta = _meta(connection)
        assert meta["schema_version"] == "4"
        assert meta["owner_profile_id"] == "default"
        assert meta["store_id"] == "profile:default"

        source = connection.execute(
            "SELECT owner_profile_id FROM sources WHERE id = ?", (LOCAL_SOURCE_ID,)
        ).fetchone()
        assert source["owner_profile_id"] == "default"
        assert {
            str(row["owner_profile_id"])
            for row in connection.execute("SELECT owner_profile_id FROM calendars")
        } == {"default"}
        assert _legacy_count(connection, "calendars", "owner_profile_id") == 0
        assert _legacy_count(connection, "events", "owner_profile_id") == 0
        assert _legacy_count(connection, "change_log", "actor") == 0

        context = connection.execute("SELECT * FROM change_context WHERE id = 1").fetchone()
        assert context["origin"] == "system"
        assert context["actor"] == "default"
        assert context["batch_id"] == "batch-v3"
        assert context["suppress"] == 0

        actors = [str(row["actor"]) for row in connection.execute("SELECT actor FROM change_log")]
        assert actors.count("default") == 53
        assert actors.count("other") == 2

        backups = _backups(path)
        assert len(backups) == 1
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        backup = sqlite3.connect(backups[0])
        backup.row_factory = sqlite3.Row
        try:
            assert _meta(backup)["owner_profile_id"] == ".hermes"
            assert _meta(backup)["store_id"] == "profile:.hermes"
            assert _legacy_count(backup, "events", "owner_profile_id") == 53
            assert _legacy_count(backup, "change_log", "actor") == 53
        finally:
            backup.close()

        # A second open is a no-op and does not create a second backup.
        again = migrate(
            connection,
            store_scope="profile",
            store_id="profile:default",
            owner_profile_id="default",
        )
        assert again == 4
        assert len(_backups(path)) == 1
    finally:
        connection.close()


def test_v4_does_not_touch_a_correctly_named_store_or_create_a_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "calendar.db"
    connection = _v3_profile_store(path, owner="coding", legacy_actors=False)
    try:
        before = _table_counts(connection)

        migrate(
            connection,
            store_scope="profile",
            store_id="profile:coding",
            owner_profile_id="coding",
        )

        assert _table_counts(connection) == before
        meta = _meta(connection)
        assert meta["schema_version"] == "4"
        assert meta["owner_profile_id"] == "coding"
        assert meta["store_id"] == "profile:coding"
        assert _legacy_count(connection, "events", "owner_profile_id") == 0
        actors = {str(row["actor"]) for row in connection.execute("SELECT actor FROM change_log")}
        assert actors == {"coding", "other"}
        assert _backups(path) == []
    finally:
        connection.close()


def test_v4_fresh_store_creates_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "calendar.db"
    connection = connect(path)
    try:
        migrate(
            connection,
            store_scope="profile",
            store_id="profile:default",
            owner_profile_id="default",
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _backups(path) == []
    finally:
        connection.close()


def test_v4_global_store_bumps_the_version_without_a_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "calendar-global.db"
    connection = connect(path)
    try:
        migrate(connection, store_scope="global", store_id="global", owner_profile_id="")
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _meta(connection)["schema_version"] == "4"
        assert _backups(path) == []
    finally:
        connection.close()


def test_v4_backup_failure_aborts_and_leaves_the_store_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "calendar.db"
    connection = _v3_profile_store(path)
    real_backup = migrations._backup_database

    def failing_backup(_connection: sqlite3.Connection) -> Path:
        raise MigrationError("cannot create the pre-v4 backup (test)")

    monkeypatch.setattr(migrations, "_backup_database", failing_backup)
    try:
        with pytest.raises(MigrationError, match="cannot create the pre-v4 backup"):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:default",
                owner_profile_id="default",
            )

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert _meta(connection)["owner_profile_id"] == ".hermes"
        assert _legacy_count(connection, "events", "owner_profile_id") == 53
        assert int(connection.execute("SELECT count(*) FROM change_log").fetchone()[0]) == 55
        assert _backups(path) == []

        monkeypatch.setattr(migrations, "_backup_database", real_backup)
        migrate(
            connection,
            store_scope="profile",
            store_id="profile:default",
            owner_profile_id="default",
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _legacy_count(connection, "events", "owner_profile_id") == 0
    finally:
        connection.close()


def test_v4_concurrent_first_open_applies_once(tmp_path: Path) -> None:
    path = tmp_path / "calendar.db"
    builder = _v3_profile_store(path)
    builder.close()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def open_once() -> None:
        connection = connect(path)
        try:
            barrier.wait(timeout=5)
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:default",
                owner_profile_id="default",
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=open_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _legacy_count(connection, "events", "owner_profile_id") == 0
        assert int(connection.execute("SELECT count(*) FROM change_log").fetchone()[0]) == 55
    finally:
        connection.close()
    assert len(_backups(path)) == 1


def test_v4_backup_write_failure_aborts_the_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "calendar.db"
    connection = _v3_profile_store(path)

    def missing_directory(_database: Path) -> Path:
        return tmp_path / "missing" / "calendar.db.pre-v4-fixed.bak"

    monkeypatch.setattr(migrations, "_backup_path", missing_directory)
    try:
        with pytest.raises(MigrationError, match="cannot create the pre-v4 backup"):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:default",
                owner_profile_id="default",
            )

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert _legacy_count(connection, "events", "owner_profile_id") == 53
        assert int(connection.execute("SELECT count(*) FROM change_log").fetchone()[0]) == 55
        assert _backups(path) == []
    finally:
        connection.close()


def test_v4_in_memory_store_with_legacy_rows_aborts(
    tmp_path: Path,
) -> None:
    connection = _v3_profile_store(Path(":memory:"))
    # Attaching a file database also covers the loop over database_list rows
    # when ``main`` has no backing file.
    connection.execute("ATTACH DATABASE ? AS extra", (str(tmp_path / "extra.db"),))
    try:
        with pytest.raises(MigrationError, match="in-memory"):
            migrate(
                connection,
                store_scope="profile",
                store_id="profile:default",
                owner_profile_id="default",
            )

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert _legacy_count(connection, "events", "owner_profile_id") == 53
    finally:
        connection.close()


def test_v4_never_overwrites_an_existing_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "calendar.db"
    connection = _v3_profile_store(path)
    stamp = "20260101T000000123456Z"
    existing = path.with_name(f"{path.name}.pre-v4-{stamp}.bak")
    existing.write_bytes(b"older backup")

    class _FixedDatetime:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            del tz
            return datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)

    monkeypatch.setattr(migrations, "datetime", _FixedDatetime)
    try:
        migrate(
            connection,
            store_scope="profile",
            store_id="profile:default",
            owner_profile_id="default",
        )

        assert existing.read_bytes() == b"older backup"
        backups = _backups(path)
        assert len(backups) == 2
        fresh = [item for item in backups if item != existing]
        assert len(fresh) == 1
        assert stat.S_IMODE(fresh[0].stat().st_mode) == 0o600
    finally:
        connection.close()
