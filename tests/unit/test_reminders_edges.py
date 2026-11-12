"""Edge-path tests: defensive branches, configuration, and degraded stores."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import time
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_calendar.db import coordination
from hermes_calendar.db.reminders import (
    DeliveryRecord,
    EventDefinition,
    InstanceDraft,
    NotificationNotFoundError,
    ReminderDefinition,
    ReminderInstance,
    ReminderStore,
    _instance_from_row,
    _load_object,
    _load_strings,
)
from hermes_calendar.models import Reminder
from hermes_calendar.reminders import service
from hermes_calendar.reminders.config import load_settings, settings_from_mapping
from hermes_calendar.reminders.engine import ReminderEngine, _zone
from hermes_calendar.reminders.models import (
    Availability,
    DeliveryResult,
    Notification,
    NotificationSettings,
    QuietHours,
)
from hermes_calendar.reminders.render import render_notification
from tests.unit.reminder_support import (
    FakeClock,
    FakeRunner,
    create_calendar,
    create_timed_event,
    enable_chat,
    make_engine,
    profile_store,
)

BASE = 1_800_000_000


def _definition(**overrides: Any) -> EventDefinition:
    base = EventDefinition(
        id="evt",
        summary="Edge",
        status="CONFIRMED",
        transparency="OPAQUE",
        start_utc=BASE,
        end_utc=BASE + 600,
        start_local="2027-01-15T08:00:00",
        start_tz="UTC",
        duration_seconds=600,
        start_date=None,
        end_date=None,
        is_all_day=False,
        rrule=None,
        rdate=(),
        exdate=(),
        parent_id=None,
        recurrence_key=None,
        xprops={},
        reminders=(),
    )
    return replace(base, **overrides)


class _StubStore:
    def __init__(
        self, definitions: list[EventDefinition], overrides: list[EventDefinition]
    ) -> None:
        self._definitions = definitions
        self._overrides = overrides
        self.applied: list[InstanceDraft] = []

    def event_definitions(self) -> list[EventDefinition]:
        return self._definitions

    def override_definitions(self) -> list[EventDefinition]:
        return self._overrides

    def apply_materialization(
        self,
        store_id: str,
        drafts: list[InstanceDraft],
        *,
        window_end_utc: int,
        now: int,
    ) -> int:
        self.applied = drafts
        return 0


class _UnavailableChat:
    name = "hermes_send"

    def available(self) -> Availability:
        return Availability(False, "no target")

    def send(self, notification: Notification) -> DeliveryResult:
        raise AssertionError("must not send")


def test_helper_loaders_and_bad_state() -> None:
    assert _load_strings(None) == ()
    assert _load_strings('["a", "b"]') == ("a", "b")
    assert _load_object(None) == {}
    with pytest.raises(ValueError, match="unknown reminder state"):
        _instance_from_row(
            cast(
                sqlite3.Row,
                {
                    "state": "bogus",
                    "id": "x",
                    "store_id": "s",
                    "event_id": "e",
                    "reminder_id": "r",
                    "recurrence_key": "k",
                    "occurrence_start_utc": 0,
                    "offset_seconds": 0,
                    "fire_at_utc": 0,
                    "priority": "normal",
                    "channel_policy_json": "{}",
                    "attempt": 0,
                    "lease_until_utc": None,
                    "snooze_until_utc": None,
                    "dedup_key": "d",
                    "created_utc": 0,
                    "updated_utc": 0,
                },
            )
        )


def test_store_defensive_paths(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        store = ReminderStore(connection)
        assert store.connection is connection
        assert store.get_event("missing") is None
        assert store.claim_due(BASE, store_ids=(), lease_seconds=1) == []
        assert store.defer_quiet_hours(BASE, until_utc=BASE, priorities=(), store_ids=()) == 0
        assert store.recover_expired_leases(BASE, store_ids=()) == 0
        with pytest.raises(NotificationNotFoundError):
            store.set_channel("missing", enabled=True, config={})
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        make_engine(connection, clock=FakeClock(BASE)).tick()
        instance = store.list_instances()[0]
        store.mark_cancelled(instance.id, BASE)
        store.mark_failed(instance.id, BASE)
        assert len(store.deliveries_for(instance.id)) == 1
    finally:
        connection.close()


def test_apply_materialization_rolls_back_on_constraint_failure(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        store = ReminderStore(connection)
        malformed = InstanceDraft(
            store_id="profile:alice",
            event_id="evt",
            reminder_id="rem",
            recurrence_key="T:1",
            occurrence_start_utc=BASE,
            offset_seconds=0,
            fire_at_utc=BASE,
            priority="bogus",
            channel_policy={},
            dedup_key="bad",
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.apply_materialization(
                "profile:alice", [malformed], window_end_utc=BASE + 100, now=BASE
            )
        assert connection.execute("SELECT count(*) FROM reminder_instances").fetchone()[0] == 0
    finally:
        connection.close()


class _ZeroRowConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if "SET state = 'firing'" in sql:
            return cast(sqlite3.Cursor, _FakeCursor(0))
        return self._connection.execute(sql, parameters)


class _FailingUpdateConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if "SET state = 'firing'" in sql:
            raise sqlite3.OperationalError("write failed")
        return self._connection.execute(sql, parameters)


class _FakeCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def test_claim_skips_a_row_it_could_not_update(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        make_engine(connection, clock=FakeClock(BASE - 600)).tick()
        store = ReminderStore(cast(sqlite3.Connection, _ZeroRowConnection(connection)))
        assert store.claim_due(BASE, store_ids=("profile:alice",), lease_seconds=1) == []
    finally:
        connection.close()


def test_claim_rolls_back_and_reraises(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        make_engine(connection, clock=FakeClock(BASE - 600)).tick()
        store = ReminderStore(cast(sqlite3.Connection, _FailingUpdateConnection(connection)))
        with pytest.raises(sqlite3.OperationalError):
            store.claim_due(BASE, store_ids=("profile:alice",), lease_seconds=1)
    finally:
        connection.close()


def test_materialize_defensive_paths(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        engine = make_engine(connection, clock=FakeClock(BASE))
        valid_override = _definition(
            id="override",
            parent_id="evt",
            recurrence_key="T:1",
            start_utc=BASE + 60,
        )
        stub = _StubStore(
            [
                _definition(),  # valid master, no reminders -> no instance
                _definition(id="no-start", start_utc=None),
                _definition(id="no-local", start_local=None),
                _definition(
                    id="bad-reminder",
                    reminders=(
                        ReminderDefinition(
                            id="r",
                            action="DISPLAY",
                            related="START",
                            trigger_kind="ABSOLUTE",
                            offset_seconds=None,
                            absolute_utc=None,
                            repeat_count=0,
                            repeat_interval_seconds=None,
                        ),
                    ),
                ),
                _definition(id="all-day-missing", is_all_day=True, start_utc=None),
            ],
            [
                _definition(id="no-parent", recurrence_key="T:1"),
                _definition(id="no-key", parent_id="evt"),
                valid_override,
                _definition(
                    id="bad-override",
                    parent_id="evt",
                    recurrence_key="T:2",
                    start_local=None,
                ),
                _definition(
                    id="good-all-day-override",
                    parent_id="evt",
                    recurrence_key="D:2027-01-16",
                    is_all_day=True,
                    start_utc=None,
                    start_date="2027-01-16",
                    end_date="2027-01-17",
                ),
                _definition(
                    id="bad-all-day-override",
                    parent_id="evt",
                    recurrence_key="D:2027-01-15",
                    is_all_day=True,
                    start_utc=None,
                    start_date=None,
                    end_date=None,
                ),
            ],
        )
        errors = engine._materialize(cast(ReminderStore, stub), "profile:alice", BASE)
        assert errors == 5
        assert stub.applied == []
    finally:
        connection.close()


def test_deliver_cancels_a_vanished_or_cancelled_event(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        event = create_timed_event(connection, calendar, start_utc=BASE + 600)
        make_engine(connection, clock=FakeClock(BASE - 600)).tick()
        engine = make_engine(connection, clock=FakeClock(BASE))
        instance = ReminderStore(connection).list_instances()[0]

        assert engine._deliver(replace(instance, event_id="missing"), BASE, True) == "cancelled"
        connection.execute("UPDATE events SET status = 'CANCELLED' WHERE id = ?", (event.id,))
        assert engine._deliver(instance, BASE, True) == "cancelled"
        global_instance = replace(instance, store_id="global")
        assert engine._deliver(global_instance, BASE, False) == "cancelled"
    finally:
        connection.close()


def test_unavailable_chat_is_logged_and_falls_back_to_in_app(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        engine = ReminderEngine(
            profile_connection=connection,
            profile_id="alice",
            settings=NotificationSettings(jitter_ratio=0.0),
            chat_notifier=_UnavailableChat(),
            clock=FakeClock(BASE),
        )
        result = engine.tick()
        assert result.delivered == 1
        row = connection.execute(
            "SELECT channel, outcome, error FROM notification_deliveries ORDER BY id"
        ).fetchall()[1]
        assert row["channel"] == "hermes_send"
        assert row["outcome"] == "permanent"
        assert "channel unavailable" in str(row["error"])
    finally:
        connection.close()


def test_retry_budget_exhaustion_falls_back_to_in_app(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner(returncode=1, stdout="", stderr="503 unavailable")
        engine = make_engine(
            connection,
            clock=FakeClock(BASE),
            runner=runner,
            settings=NotificationSettings(max_attempts=1, jitter_ratio=0.0),
        )
        result = engine.tick()
        assert result.delivered == 1
        assert ReminderStore(connection).list_instances()[0].state == "delivered"
    finally:
        connection.close()


def test_ledger_write_failure_keeps_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.reminder_support import global_store

    shared = global_store(tmp_path)
    connection = profile_store(tmp_path)
    try:
        from hermes_calendar.db import Repository

        create_timed_event(shared, Repository(shared).list_calendars()[0], start_utc=BASE + 600)
        enable_chat(connection)

        def failing_ledger(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("ledger down")

        monkeypatch.setattr(coordination, "record_ledger", failing_ledger)
        engine = make_engine(
            connection,
            clock=FakeClock(BASE),
            runner=FakeRunner(),
            global_connection=shared,
        )
        result = engine.tick()
        assert result.delivered == 1
        assert ReminderStore(connection).list_instances()[0].state == "delivered"
    finally:
        connection.close()
        shared.close()


def test_daytime_quiet_window_uses_the_same_day(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        # 10:00 UTC on 2027-01-15, inside a 09:00-17:00 window.
        daytime = 1_800_007_200
        create_timed_event(
            connection,
            calendar,
            start_utc=daytime + 600,
            reminders=(Reminder(offset_seconds=0),),
        )
        settings = NotificationSettings(
            quiet_hours=QuietHours(start=time(9, 0), end=time(17, 0)),
            jitter_ratio=0.0,
        )
        result = make_engine(connection, clock=FakeClock(daytime + 600), settings=settings).tick()
        assert result.quiet_deferred == 1
        assert _instance_snooze(connection) == daytime + 7 * 3_600
    finally:
        connection.close()


def _instance_snooze(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT snooze_until_utc FROM reminder_instances").fetchone()[0])


def test_quiet_window_with_everything_allowed_does_not_defer(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        quiet_base = 1_800_050_400
        create_timed_event(
            connection,
            calendar,
            start_utc=quiet_base,
            reminders=(Reminder(offset_seconds=0),),
        )
        settings = NotificationSettings(
            quiet_hours=QuietHours(
                start=time(21, 30),
                end=time(7, 0),
                allow=frozenset({"digest", "normal", "high", "critical"}),
            ),
            jitter_ratio=0.0,
        )
        result = make_engine(
            connection,
            clock=FakeClock(quiet_base),
            settings=settings,
        ).tick()
        assert result.quiet_deferred == 0
    finally:
        connection.close()


def test_unknown_timezone_falls_back_to_utc_directly() -> None:
    assert str(_zone("Not/AZone")) == "UTC"


def test_unknown_timezone_falls_back_to_utc(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        engine = make_engine(
            connection,
            clock=FakeClock(BASE),
            settings=NotificationSettings(timezone="Not/AZone", jitter_ratio=0.0),
        )
        assert engine.tick().delivered == 1
    finally:
        connection.close()


def test_transparent_event_is_digest_and_stays_in_app(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600, transparency="TRANSPARENT")
        enable_chat(connection)
        runner = FakeRunner()
        result = make_engine(connection, clock=FakeClock(BASE), runner=runner).tick()
        assert result.delivered == 1
        assert runner.calls == []
        assert ReminderStore(connection).list_instances()[0].priority == "digest"
    finally:
        connection.close()


def test_catch_up_prefixes_while_you_were_away(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        enable_chat(connection)
        runner = FakeRunner()
        make_engine(connection, clock=FakeClock(BASE + 180), runner=runner).tick()
        assert "While you were away" in runner.bodies[0]
    finally:
        connection.close()


def test_run_tick_degrades_when_the_global_store_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_calendar.reminders import runtime

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))

    def failing_global() -> None:
        raise sqlite3.OperationalError("global locked")

    monkeypatch.setattr(runtime, "open_global_store", failing_global)
    result = runtime.run_tick(now=BASE)
    assert result.profile_id == "alice"
    assert result.global_lease_holder is False


def test_engine_settings_property_and_backoff_clamp(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        settings = NotificationSettings(backoff_seconds=(10,), jitter_ratio=0.0)
        engine = make_engine(connection, clock=FakeClock(BASE), settings=settings)
        assert engine.settings is settings
        assert engine._backoff(99) == 10
    finally:
        connection.close()


def test_settings_mapping_valid_and_malformed() -> None:
    default = settings_from_mapping({})
    assert default.timezone == "UTC"
    assert default.backoff_seconds == (10, 30, 120, 600, 1800)
    assert default.in_process_ticker_enabled is False

    configured = settings_from_mapping(
        {
            "timezone": "America/Chicago",
            "stale_grace_minutes": True,
            "max_late_minutes": -5,
            "materialize_horizon_days": 0,
            "all_day_fire_time": "not-a-time",
            "quiet_hours": {"start": "bad", "end": "08:15", "allow": ["normal"]},
            "dashboard": {"public_url": "https://dash.example/"},
            "backoff_seconds": [True, 5],
            "jitter_ratio": 0,
            "in_process_ticker": {"enabled": True, "interval_seconds": 2},
        }
    )
    assert configured.timezone == "America/Chicago"
    assert configured.stale_grace_minutes == 10
    assert configured.max_late_minutes == 0
    assert configured.materialize_horizon_days == 1
    assert configured.all_day_fire_time == time(7, 0)
    assert configured.quiet_hours is not None
    assert configured.quiet_hours.start == time(21, 30)
    assert configured.quiet_hours.end == time(8, 15)
    assert configured.quiet_hours.allow == frozenset({"normal"})
    assert configured.dashboard_url == "https://dash.example"
    assert configured.backoff_seconds == (5,)
    assert configured.in_process_ticker_enabled is True
    assert configured.ticker_interval_seconds == 5

    flat_url = settings_from_mapping({"dashboard_url": "https://flat.example/"})
    assert flat_url.dashboard_url == "https://flat.example"
    assert settings_from_mapping({"dashboard_url": ""}).dashboard_url is None
    assert settings_from_mapping({"quiet_hours": "bogus"}).quiet_hours is None
    assert settings_from_mapping({"quiet_hours": {"allow": "bogus"}}).quiet_hours is not None


def test_load_settings_ignores_a_non_mapping_pref(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        store = ReminderStore(connection)
        assert load_settings(store).timezone == "UTC"
        store.set_pref("notifications", ["not", "a", "mapping"])
        assert load_settings(store).timezone == "UTC"
    finally:
        connection.close()


def test_quiet_hours_contains_boundaries() -> None:
    normal = QuietHours(start=time(21, 30), end=time(7, 0))
    assert normal.contains(time(22, 0)) is True
    assert normal.contains(time(6, 59)) is True
    assert normal.contains(time(7, 0)) is False
    assert normal.contains(time(12, 0)) is False
    same = QuietHours(start=time(3, 0), end=time(3, 0))
    assert same.contains(time(3, 0)) is False
    daytime = QuietHours(start=time(9, 0), end=time(17, 0))
    assert daytime.contains(time(10, 0)) is True


def test_service_fallbacks_and_direct_validation(tmp_path: Path) -> None:
    connection = profile_store(tmp_path)
    try:
        make_engine(connection, clock=FakeClock(BASE - 60))
        store = ReminderStore(connection)
        settings = NotificationSettings()
        missing = ReminderInstance(
            id="x",
            store_id="profile:alice",
            event_id="missing",
            reminder_id="r",
            recurrence_key="T:1",
            occurrence_start_utc=BASE,
            offset_seconds=-600,
            fire_at_utc=BASE,
            priority="normal",
            channel_policy={},
            state="scheduled",
            attempt=0,
            lease_until_utc=None,
            snooze_until_utc=None,
            dedup_key="d",
            created_utc=BASE,
            updated_utc=BASE,
        )
        assert service.notification_payload(missing, store, None)["title"] == "Calendar reminder"
        global_missing = replace(missing, store_id="global")
        assert (
            service.notification_payload(global_missing, store, None)["title"]
            == "Calendar reminder"
        )
        assert (
            service.notification_payload(missing, store, None, include_deliveries=False).get(
                "last_delivery"
            )
            is None
        )
        with pytest.raises(ValueError, match="minutes must be"):
            service.snooze_notification(connection, notification_id="x", minutes=7, now=BASE)
        rendered = render_notification(missing, summary="", settings=settings)
        assert rendered.title == "Calendar reminder in 10 min"
        payload = service._delivery_payload(
            DeliveryRecord(
                id=1,
                reminder_instance_id="x",
                channel="hermes_send",
                target="t",
                attempt=1,
                started_utc=BASE,
                finished_utc=None,
                outcome="unknown",
                exit_code=None,
                provider_id=None,
                error=None,
                latency_ms=None,
            )
        )
        assert payload["finished"] is None
    finally:
        connection.close()


def test_engine_deliver_marks_cancelled_when_global_store_is_missing(
    tmp_path: Path,
) -> None:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        make_engine(connection, clock=FakeClock(BASE - 60)).tick()
        engine = make_engine(connection, clock=FakeClock(BASE))
        instance = ReminderStore(connection).list_instances()[0]
        assert engine._deliver(replace(instance, store_id="global"), BASE, True) == "cancelled"
    finally:
        connection.close()
