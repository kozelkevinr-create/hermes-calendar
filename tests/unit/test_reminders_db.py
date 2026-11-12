"""Schema v2, claim/lease, and notification-store tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_calendar.db import connect
from hermes_calendar.db.migrations import (
    SCHEMA_VERSION,
    V1_SCHEMA_VERSION,
    migrate,
)
from hermes_calendar.db.reminders import (
    NotificationNotFoundError,
    ReminderStore,
)
from hermes_calendar.db.schema import V1_STATEMENTS
from tests.unit.reminder_support import (
    FakeClock,
    create_calendar,
    create_timed_event,
    global_store,
    make_engine,
    profile_store,
)

BASE = 1_800_000_000


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_profile_v2_adds_notification_tables_and_channels(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        tables = _table_names(connection)
        assert {
            "reminder_instances",
            "notification_deliveries",
            "notification_channels",
            "notification_prefs",
            "web_push_subscriptions",
        } <= tables
        assert "reminder_lease" not in tables
        channels = ReminderStore(connection).channels()
        assert [(channel.id, channel.kind, channel.enabled) for channel in channels] == [
            ("in-app", "in_app", True),
            ("phone", "hermes_send", False),
        ]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        connection.close()


def test_global_v2_has_only_coordination_tables(tmp_path: Path) -> None:
    connection = global_store(tmp_path)
    try:
        tables = _table_names(connection)
        assert {"reminder_lease", "reminder_delivery_ledger"} <= tables
        assert "reminder_instances" not in tables
        assert "notification_deliveries" not in tables
    finally:
        connection.close()


def test_v1_store_upgrades_to_v2_in_place(tmp_path: Path) -> None:
    connection = connect(tmp_path / "legacy.db")
    try:
        for statement in V1_STATEMENTS:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
            (
                ("schema_version", str(V1_SCHEMA_VERSION)),
                ("created_by", "hermes-calendar"),
                ("min_available_seq", "1"),
                ("store_scope", "profile"),
                ("store_id", "profile:legacy"),
                ("owner_profile_id", "legacy"),
            ),
        )
        connection.execute(f"PRAGMA user_version = {V1_SCHEMA_VERSION}")

        version = migrate(
            connection,
            store_scope="profile",
            store_id="profile:legacy",
            owner_profile_id="legacy",
        )

        assert version == SCHEMA_VERSION
        assert "reminder_instances" in _table_names(connection)
        values = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM schema_meta")
        }
        assert values["schema_version"] == str(SCHEMA_VERSION)
    finally:
        connection.close()


def test_claim_due_is_single_flight_across_connections(tmp_path: Path) -> None:
    writer = profile_store(tmp_path)
    claimer = connect(tmp_path / "alice.db")
    try:
        calendar = create_calendar(writer)
        create_timed_event(writer, calendar, start_utc=BASE + 600)
        clock = FakeClock(BASE - 600)
        engine = make_engine(writer, clock=clock)
        engine.tick()

        first = ReminderStore(writer).claim_due(
            BASE, store_ids=("profile:alice",), lease_seconds=120
        )
        second = ReminderStore(claimer).claim_due(
            BASE, store_ids=("profile:alice",), lease_seconds=120
        )

        assert len(first) == 1
        assert first[0].attempt == 1
        assert first[0].state == "firing"
        assert second == []
    finally:
        claimer.close()
        writer.close()


def test_materialization_is_idempotent(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        clock = FakeClock(BASE - 3_600)
        engine = make_engine(connection, clock=clock)
        engine.tick()
        engine.tick()
        rows = connection.execute("SELECT * FROM reminder_instances").fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "scheduled"
    finally:
        connection.close()


def test_delete_event_cancels_scheduled_instance(tmp_path: Path) -> None:
    from hermes_calendar.db import Repository

    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        event = create_timed_event(connection, calendar, start_utc=BASE + 3_600)
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock)
        engine.tick()
        assert (
            connection.execute("SELECT state FROM reminder_instances").fetchone()[0] == "scheduled"
        )

        Repository(connection).delete_event(event.id)
        result = engine.tick()

        assert result.claimed == 0
        assert (
            connection.execute("SELECT state FROM reminder_instances").fetchone()[0] == "cancelled"
        )
    finally:
        connection.close()


def test_quiet_deferral_marks_snooze_without_incrementing_attempt(tmp_path: Path) -> None:
    from datetime import time

    from hermes_calendar.models import Reminder
    from hermes_calendar.reminders.models import NotificationSettings, QuietHours

    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        # 10:00 local tomorrow, reminder 12 h before = 22:00 tonight.
        quiet_base = 1_800_050_400  # 2027-01-15 22:00 UTC
        create_timed_event(
            connection,
            calendar,
            start_utc=quiet_base + 12 * 3_600,
            reminders=(Reminder(offset_seconds=-12 * 3_600),),
        )
        settings = NotificationSettings(
            quiet_hours=QuietHours(start=time(21, 30), end=time(7, 0)),
            jitter_ratio=0.0,
        )
        clock = FakeClock(quiet_base)
        engine = make_engine(connection, clock=clock, settings=settings)
        result = engine.tick()
        assert result.quiet_deferred == 1
        row = connection.execute("SELECT * FROM reminder_instances").fetchone()
        assert row["state"] == "scheduled"
        assert row["attempt"] == 0
        assert row["snooze_until_utc"] == quiet_base + 9 * 3_600
    finally:
        connection.close()


def test_snooze_and_dismiss_transitions(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        store = ReminderStore(connection)
        with pytest.raises(NotificationNotFoundError):
            store.get_instance("missing")
        with pytest.raises(NotificationNotFoundError):
            store.snooze("missing", until_utc=BASE, now=BASE)

        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        engine = make_engine(connection, clock=FakeClock(BASE))
        engine.tick()
        instance = store.list_instances()[0]
        store.snooze(instance.id, until_utc=BASE + 300, now=BASE)
        assert store.get_instance(instance.id).state == "scheduled"
        store.dismiss(instance.id, BASE)
        assert store.get_instance(instance.id).state == "acknowledged"
        with pytest.raises(NotificationNotFoundError):
            store.snooze(instance.id, until_utc=BASE + 300, now=BASE)
    finally:
        connection.close()


def test_expired_lease_is_recovered_and_recorded_unknown(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        engine = make_engine(connection, clock=FakeClock(BASE - 600))
        engine.tick()
        instance_id = str(connection.execute("SELECT id FROM reminder_instances").fetchone()[0])
        connection.execute(
            """
            UPDATE reminder_instances
               SET state = 'firing', lease_until_utc = ?, attempt = 1
             WHERE id = ?
            """,
            (BASE - 1, instance_id),
        )

        recovered = ReminderStore(connection).recover_expired_leases(
            BASE, store_ids=("profile:alice",)
        )

        assert recovered == 1
        assert ReminderStore(connection).get_instance(instance_id).state == "retrying"
        delivery = ReminderStore(connection).latest_delivery(instance_id)
        assert delivery is not None
        assert delivery.channel == "recovery"
        assert delivery.outcome == "unknown"
    finally:
        connection.close()
