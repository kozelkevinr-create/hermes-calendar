"""Reminder engine acceptance tests with a fake clock and fake ``hermes send``."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from pathlib import Path
from typing import cast

import pytest

from hermes_calendar.db import Repository, connect
from hermes_calendar.db.reminders import ReminderStore
from hermes_calendar.models import Attendee, Reminder
from hermes_calendar.reminders import service
from hermes_calendar.reminders.adapters import build_chat_notifier
from hermes_calendar.reminders.engine import ProfileMismatchError, ReminderEngine
from hermes_calendar.reminders.models import NotificationSettings, QuietHours
from tests.unit.reminder_support import (
    FakeClock,
    FakeRunner,
    create_all_day_event,
    create_calendar,
    create_timed_event,
    enable_chat,
    make_engine,
    profile_store,
)

BASE = 1_800_000_000


def _instance(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM reminder_instances").fetchone()
    assert row is not None
    return cast(sqlite3.Row, row)


def test_due_reminder_is_delivered_once_through_hermes_send(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock, runner=runner)

        first = engine.tick()
        second = engine.tick()

        assert first.claimed == 1
        assert first.delivered == 1
        assert second.claimed == 0
        assert len(runner.calls) == 1
        assert runner.calls[0][:2] == ["hermes", "send"]
        assert "--to=matrix:@alice:example.org" in runner.calls[0]
        assert "--json" in runner.calls[0]
        assert _instance(connection)["state"] == "delivered"
        deliveries = connection.execute(
            "SELECT channel, outcome FROM notification_deliveries ORDER BY id"
        ).fetchall()
        assert [(row["channel"], row["outcome"]) for row in deliveries] == [
            ("in_app", "success"),
            ("hermes_send", "success"),
        ]
    finally:
        connection.close()


def test_two_tickers_claiming_the_same_row_deliver_once(tmp_path: Path) -> None:
    first_connection = profile_store(tmp_path)
    second_connection = connect(tmp_path / "alice.db")
    try:
        calendar = create_calendar(first_connection)
        create_timed_event(first_connection, calendar, start_utc=BASE + 600)
        enable_chat(first_connection)
        runner = FakeRunner()
        clock = FakeClock(BASE)
        first = make_engine(first_connection, clock=clock, runner=runner)
        second = make_engine(second_connection, clock=clock, runner=runner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda engine: engine.tick(), (first, second)))

        assert sum(result.claimed for result in results) == 1
        assert len(runner.calls) == 1
        assert _instance(first_connection)["state"] == "delivered"
    finally:
        second_connection.close()
        first_connection.close()


def test_recurring_event_expands_the_next_occurrence(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE - 6 * 86_400,
            rrule="FREQ=WEEKLY;COUNT=4",
        )
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE + 86_400 - 600)
        engine = make_engine(connection, clock=clock, runner=runner)

        result = engine.tick()

        assert result.claimed == 1
        rows = connection.execute(
            "SELECT recurrence_key, state FROM reminder_instances ORDER BY recurrence_key"
        ).fetchall()
        assert len(rows) == 3
        assert all(str(row["recurrence_key"]).startswith("T:") for row in rows)
        assert [row["state"] for row in rows].count("delivered") == 1
        assert len(runner.calls) == 1
    finally:
        connection.close()


def test_all_day_reminder_fires_at_the_configured_local_morning(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_all_day_event(
            connection,
            calendar,
            day=date(2027, 1, 16),
            reminders=(Reminder(offset_seconds=-86_400),),
        )
        enable_chat(connection)
        runner = FakeRunner()
        morning = 1_800_050_400 - 9 * 3_600  # 2027-01-15 13:00 UTC is 07:00 in Chicago
        settings = NotificationSettings(timezone="America/Chicago", jitter_ratio=0.0)
        clock = FakeClock(morning)
        engine = make_engine(connection, clock=clock, runner=runner, settings=settings)

        result = engine.tick()

        assert result.delivered == 1
        assert len(runner.calls) == 1
        fire_at = int(_instance(connection)["fire_at_utc"])
        assert fire_at == morning
    finally:
        connection.close()


def test_repeat_count_materializes_extra_instances(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE + 600,
            reminders=(Reminder(offset_seconds=-600, repeat_count=2, repeat_interval_seconds=300),),
        )
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock, runner=runner)

        engine.tick()
        clock.advance(300)
        engine.tick()

        rows = connection.execute(
            "SELECT recurrence_key, state FROM reminder_instances ORDER BY recurrence_key"
        ).fetchall()
        assert len(rows) == 3
        assert len(runner.calls) == 2
    finally:
        connection.close()


def test_snooze_delivers_again_after_the_snooze_time(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock, runner=runner)
        engine.tick()
        instance_id = str(_instance(connection)["id"])

        service.snooze_notification(
            connection, notification_id=instance_id, minutes=5, now=BASE + 60
        )
        clock.advance(360)
        result = engine.tick()

        assert result.delivered == 1
        assert len(runner.calls) == 2
        assert ReminderStore(connection).get_instance(instance_id).state == "delivered"
    finally:
        connection.close()


def test_dismiss_hides_the_notification_and_stops_delivery(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock, runner=runner)
        engine.tick()
        instance_id = str(_instance(connection)["id"])

        service.dismiss_notification(connection, notification_id=instance_id, now=BASE + 1)
        hidden = service.list_notifications(connection)
        visible = service.list_notifications(connection, include_dismissed=True)
        clock.advance(600)
        second = engine.tick()

        assert hidden == []
        assert [item["id"] for item in visible] == [instance_id]
        assert second.claimed == 0
        assert len(runner.calls) == 1
    finally:
        connection.close()


def test_quiet_hours_defer_normal_until_the_window_ends(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        quiet_base = 1_800_050_400  # 22:00 UTC
        create_timed_event(
            connection,
            calendar,
            start_utc=quiet_base + 10 * 3_600,
            reminders=(Reminder(offset_seconds=-10 * 3_600),),
        )
        enable_chat(connection)
        runner = FakeRunner()
        settings = NotificationSettings(
            quiet_hours=QuietHours(start=time(21, 30), end=time(7, 0)),
            jitter_ratio=0.0,
        )
        clock = FakeClock(quiet_base)
        engine = make_engine(connection, clock=clock, runner=runner, settings=settings)

        deferred = engine.tick()
        assert deferred.quiet_deferred == 1
        assert deferred.claimed == 0
        assert runner.calls == []

        clock.advance(9 * 3_600)
        delivered = engine.tick()
        assert delivered.delivered == 1
        assert len(runner.calls) == 1
        assert "While you were away" not in runner.bodies[0]
    finally:
        connection.close()


def test_quiet_hours_never_defer_critical(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        quiet_base = 1_800_050_400
        create_timed_event(
            connection,
            calendar,
            start_utc=quiet_base + 600,
            xprops={"hermes_calendar.priority": "critical"},
        )
        enable_chat(connection)
        runner = FakeRunner()
        settings = NotificationSettings(
            quiet_hours=QuietHours(start=time(21, 30), end=time(7, 0)),
            jitter_ratio=0.0,
        )
        engine = make_engine(
            connection, clock=FakeClock(quiet_base), runner=runner, settings=settings
        )

        result = engine.tick()

        assert result.quiet_deferred == 0
        assert result.delivered == 1
        assert len(runner.calls) == 1
    finally:
        connection.close()


def test_stale_event_is_suppressed_not_delivered_late(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE,
            reminders=(Reminder(offset_seconds=-12 * 60),),
        )
        enable_chat(connection)
        runner = FakeRunner()
        engine = make_engine(connection, clock=FakeClock(BASE + 720), runner=runner)

        result = engine.tick()

        assert result.suppressed == 1
        assert runner.calls == []
        assert _instance(connection)["state"] == "suppressed"
    finally:
        connection.close()


def test_reminder_older_than_the_late_limit_is_suppressed(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE + 2 * 3_600,
            reminders=(Reminder(offset_seconds=-3 * 3_600),),
        )
        enable_chat(connection)
        runner = FakeRunner()
        engine = make_engine(connection, clock=FakeClock(BASE + 60), runner=runner)

        result = engine.tick()

        assert result.suppressed == 1
        assert runner.calls == []
    finally:
        connection.close()


def test_permanent_send_failure_is_visible_and_falls_back_to_in_app(
    tmp_path: Path,
) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner(returncode=2, stdout="", stderr="usage")
        engine = make_engine(connection, clock=FakeClock(BASE), runner=runner)

        result = engine.tick()

        assert result.delivered == 1
        rows = connection.execute(
            "SELECT channel, outcome, exit_code FROM notification_deliveries ORDER BY id"
        ).fetchall()
        assert (rows[0]["channel"], rows[0]["outcome"]) == ("in_app", "success")
        assert (rows[1]["channel"], rows[1]["outcome"], rows[1]["exit_code"]) == (
            "hermes_send",
            "permanent",
            2,
        )
        assert _instance(connection)["state"] == "delivered"
    finally:
        connection.close()


def test_transient_send_failure_retries_then_delivers(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner(returncode=1, stdout="", stderr="503 service unavailable")
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock, runner=runner)

        first = engine.tick()
        assert first.retrying == 1
        row = _instance(connection)
        assert row["state"] == "retrying"
        assert int(row["snooze_until_utc"]) == BASE + 10

        runner.returncode = 0
        clock.advance(10)
        second = engine.tick()

        assert second.delivered == 1
        assert len(runner.calls) == 2
        assert _instance(connection)["state"] == "delivered"
    finally:
        connection.close()


def test_missing_explicit_target_never_guesses_or_calls_hermes(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        runner = FakeRunner()
        engine = make_engine(connection, clock=FakeClock(BASE), runner=runner)

        result = engine.tick()

        assert result.delivered == 1
        assert runner.calls == []
        channels = [
            row["channel"]
            for row in connection.execute("SELECT channel FROM notification_deliveries ORDER BY id")
        ]
        assert channels == ["in_app"]
    finally:
        connection.close()


def test_chat_message_contains_minimal_sanitized_content(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE + 600,
            summary="Dentist @everyone <!channel>",
            xprops={},
        )
        connection.execute(
            "UPDATE events SET location = ?, description = ?",
            ("123 Main St", "Bring the secret paperwork"),
        )
        enable_chat(connection)
        runner = FakeRunner()
        settings = NotificationSettings(
            dashboard_url="https://calendar.example.test", jitter_ratio=0.0
        )
        engine = make_engine(connection, clock=FakeClock(BASE), runner=runner, settings=settings)

        engine.tick()

        subject = next(
            token.removeprefix("--subject=")
            for token in runner.calls[0]
            if token.startswith("--subject=")
        )
        body = runner.bodies[0]
        assert "123 Main St" not in subject + body
        assert "secret paperwork" not in subject + body
        assert "@\u200beveryone" in subject
        assert "<\u200b!channel>" in subject
        assert "https://calendar.example.test/calendar?event=" in body
        assert "127.0.0.1" not in body
    finally:
        connection.close()


def test_no_deep_link_without_a_configured_dashboard_origin(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner()
        engine = make_engine(connection, clock=FakeClock(BASE), runner=runner)

        engine.tick()

        assert "Open the calendar app" in runner.bodies[0]
        assert "event=" not in runner.bodies[0]
    finally:
        connection.close()


def test_wrong_profile_is_refused(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        with pytest.raises(ProfileMismatchError, match="refusing to tick"):
            ReminderEngine(
                profile_connection=connection,
                profile_id="bob",
                settings=NotificationSettings(),
                clock=FakeClock(BASE),
            )
    finally:
        connection.close()


def test_backward_clock_jump_skips_the_tick(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE)
        engine = make_engine(connection, clock=clock, runner=runner)
        ReminderStore(connection).set_pref("last_tick_utc", BASE + 600, BASE + 600)

        skipped = engine.tick()
        normal = engine.tick()

        assert skipped.clock_backwards is True
        assert skipped.claimed == 0
        assert normal.clock_backwards is False
        assert len(runner.calls) == 1
    finally:
        connection.close()


def test_malformed_recurrence_is_counted_not_fatal(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE + 600,
            rrule="FREQ=WEEKLY;BYDAY=NOTADAY",
        )
        engine = make_engine(connection, clock=FakeClock(BASE))

        result = engine.tick()

        assert result.materialization_errors == 1
        assert connection.execute("SELECT count(*) FROM reminder_instances").fetchone()[0] == 0
    finally:
        connection.close()


def test_end_related_and_absolute_triggers(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(
            connection,
            calendar,
            start_utc=BASE + 3_600,
            duration_seconds=3_600,
            reminders=(
                Reminder(offset_seconds=-600, related="END"),
                Reminder(trigger_kind="ABSOLUTE", absolute_utc=BASE + 120),
            ),
        )
        enable_chat(connection)
        runner = FakeRunner()
        clock = FakeClock(BASE + 120)
        engine = make_engine(connection, clock=clock, runner=runner)

        first = engine.tick()
        assert first.delivered == 1

        clock.advance(6_480)
        second = engine.tick()
        assert second.delivered == 1
        assert len(runner.calls) == 2
        fires = sorted(
            int(row["fire_at_utc"])
            for row in connection.execute("SELECT fire_at_utc FROM reminder_instances")
        )
        assert fires == [BASE + 120, BASE + 6_600]
    finally:
        connection.close()


def test_attendees_are_never_in_the_chat_message(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        from hermes_calendar.models import EventDraft
        from hermes_calendar.models.ids import new_uuid7

        draft = EventDraft(
            calendar_id=calendar.id,
            summary="Review",
            start_utc=BASE + 600,
            end_utc=BASE + 1_200,
            start_local="2027-01-15T08:10:00",
            start_tz="UTC",
            end_local="2027-01-15T08:20:00",
            end_tz="UTC",
            duration_seconds=600,
            uid=f"{new_uuid7()}@test",
            attendees=(Attendee(address="mailto:secret@example.test", name="Secret Person"),),
            reminders=(Reminder(offset_seconds=-600),),
        )
        Repository(connection).create_event(draft)
        enable_chat(connection)
        runner = FakeRunner()
        engine = make_engine(connection, clock=FakeClock(BASE), runner=runner)

        engine.tick()

        assert "Secret Person" not in runner.bodies[0]
        assert "secret@example.test" not in runner.bodies[0]
    finally:
        connection.close()


def test_build_chat_notifier_ignores_disabled_or_targetless_channels(
    tmp_path: Path,
) -> None:
    connection = profile_store(tmp_path)
    try:
        store = ReminderStore(connection)
        assert build_chat_notifier(store.channels()) is None
        store.set_channel("phone", enabled=True, config={})
        assert build_chat_notifier(store.channels()) is None
    finally:
        connection.close()
