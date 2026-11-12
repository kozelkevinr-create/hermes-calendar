"""Agent tools for listing, snoozing, and dismissing notifications."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_calendar.db.store import open_profile_store
from hermes_calendar.hermes import tools as hermes_tools
from hermes_calendar.hermes.tools import (
    calendar_dismiss_notification,
    calendar_list_notifications,
    calendar_snooze_notification,
)
from hermes_calendar.reminders import service as reminder_service
from tests.unit.reminder_support import (
    FakeClock,
    create_calendar,
    create_timed_event,
    make_engine,
)

BASE = 1_800_000_000


@pytest.fixture
def delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    home = tmp_path / "root" / "profiles" / "alice"
    monkeypatch.setenv("HERMES_HOME", str(home))
    connection = open_profile_store()
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600)
        engine = make_engine(connection, clock=FakeClock(BASE))
        engine.tick()
        instance = connection.execute("SELECT id FROM reminder_instances").fetchone()
        assert instance is not None
        return str(instance["id"]), str(home)
    finally:
        connection.close()


def _payload(result: str) -> dict[str, Any]:
    loaded: Any = json.loads(result)
    assert isinstance(loaded, dict)
    return loaded


def test_list_notifications_returns_minimal_fields(delivered: tuple[str, str]) -> None:
    instance_id, _ = delivered
    payload = _payload(calendar_list_notifications({}))
    assert payload["ok"] is True
    assert payload["scope"] == "profile"
    assert payload["count"] == 1
    item = payload["notifications"][0]
    assert item["id"] == instance_id
    assert item["title"] == "Dentist"
    assert item["state"] == "delivered"
    assert "description" not in item
    assert "location" not in item
    assert item["last_delivery"]["channel"] == "in_app"


def test_list_filters_by_state_and_limit(delivered: tuple[str, str]) -> None:
    payload = _payload(calendar_list_notifications({"state": "suppressed"}))
    assert payload["count"] == 0
    payload = _payload(calendar_list_notifications({"limit": 500}))
    assert payload["count"] == 1


def test_list_rejects_an_unknown_state(delivered: tuple[str, str]) -> None:
    payload = _payload(calendar_list_notifications({"state": "bogus"}))
    assert payload["ok"] is False
    assert payload["code"] == "invalid_argument"


def test_snooze_then_list_and_dismiss(delivered: tuple[str, str]) -> None:
    instance_id, _ = delivered
    snoozed = _payload(
        calendar_snooze_notification({"notification_id": instance_id, "minutes": 15})
    )
    assert snoozed["ok"] is True
    assert snoozed["notification"]["state"] == "scheduled"

    dismissed = _payload(calendar_dismiss_notification({"notification_id": instance_id}))
    assert dismissed["ok"] is True
    assert dismissed["notification"]["state"] == "acknowledged"

    hidden = _payload(calendar_list_notifications({}))
    assert hidden["count"] == 0
    visible = _payload(calendar_list_notifications({"include_dismissed": True}))
    assert visible["count"] == 1


def test_snooze_rejects_an_unsupported_duration(delivered: tuple[str, str]) -> None:
    instance_id, _ = delivered
    payload = _payload(calendar_snooze_notification({"notification_id": instance_id, "minutes": 7}))
    assert payload["ok"] is False
    assert payload["code"] == "invalid_argument"


def test_list_degrades_when_the_global_store_cannot_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivered: tuple[str, str],
) -> None:
    del delivered

    def failing_open() -> None:
        raise sqlite3.OperationalError("global locked")

    monkeypatch.setattr(reminder_service, "open_global_store", failing_open)
    payload = _payload(calendar_list_notifications({}))
    assert payload["ok"] is True
    assert payload["count"] == 1


def test_unknown_notification_returns_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    open_profile_store().close()
    payload = _payload(calendar_snooze_notification({"notification_id": "missing", "minutes": 5}))
    assert payload["ok"] is False
    assert payload["code"] == "not_found"

    payload = _payload(calendar_dismiss_notification({"notification_id": "missing"}))
    assert payload["ok"] is False
    assert payload["code"] == "not_found"


def test_notification_argument_validation_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    open_profile_store().close()

    payload = _payload(calendar_list_notifications({"include_dismissed": "yes"}))
    assert payload["code"] == "invalid_argument"
    payload = _payload(calendar_list_notifications({"limit": True}))
    assert payload["code"] == "invalid_argument"
    payload = _payload(calendar_list_notifications({"limit": 0}))
    assert payload["code"] == "invalid_argument"
    payload = _payload(calendar_list_notifications({"include_dismissed": False, "limit": 1}))
    assert payload["ok"] is True

    payload = _payload(calendar_snooze_notification({"notification_id": "", "minutes": 5}))
    assert payload["code"] == "invalid_argument"
    payload = _payload(
        calendar_snooze_notification({"notification_id": "x" * 20_001, "minutes": 5})
    )
    assert payload["code"] == "invalid_argument"
    payload = _payload(calendar_snooze_notification({"notification_id": "id", "minutes": "10"}))
    assert payload["code"] == "invalid_argument"
    payload = _payload(calendar_snooze_notification({"notification_id": "id", "minutes": 4}))
    assert payload["code"] == "invalid_argument"


def test_snooze_schema_uses_the_shared_allowlist() -> None:
    schema = next(
        spec[1] for spec in hermes_tools.TOOL_SPECS if spec[0] == "calendar_snooze_notification"
    )

    assert schema["parameters"]["properties"]["minutes"]["enum"] == [5, 15, 60, 1440]
    assert sorted(reminder_service.SNOOZE_MINUTES) == [5, 15, 60, 1440]
