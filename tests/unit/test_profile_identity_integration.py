"""End-to-end identity tests: status, tools, two profiles, and global creators."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_calendar import plugin
from hermes_calendar.hermes.tools import (
    calendar_create_calendar,
    calendar_create_event,
    calendar_get_event,
    calendar_list_calendars,
)
from hermes_calendar.plugin import calendar_status
from hermes_calendar.plugin_storage import global_dir
from hermes_calendar.profile_identity import ProfileIdentityError, current_profile_id
from hermes_calendar.service import CalendarService
from hermes_calendar.shared_store_check import verify_shared

PLUGIN = "hermes-calendar"


@pytest.fixture
def default_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point at the default Hermes home (``~/.hermes``)."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".hermes"


def test_calendar_status_default_profile_reports_shared_ok(default_home: Path) -> None:
    assert current_profile_id() == "default"

    payload: dict[str, Any] = json.loads(calendar_status())

    shared = payload["shared_store"]
    assert shared["ok"] is True
    assert shared["writable"] is True
    sentinel = global_dir(PLUGIN) / "_shared_check.db"
    connection = sqlite3.connect(sentinel)
    try:
        rows = connection.execute("SELECT profile_id FROM shared_sentinel").fetchall()
    finally:
        connection.close()
    assert rows == [("default",)]


def test_calendar_status_reports_identity_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch, default_home: Path
) -> None:
    def failing_identity() -> str:
        raise ProfileIdentityError("invalid profile id: got '.hermes'")

    monkeypatch.setattr(plugin, "current_profile_id", failing_identity)

    payload: dict[str, Any] = json.loads(calendar_status())

    shared = payload["shared_store"]
    assert shared["ok"] is False
    assert shared["writable"] is False
    assert shared["peers_seen"] == []
    assert "profile identity error" in shared["detail"]
    assert "invalid profile id" in shared["detail"]


def test_tools_store_and_return_the_default_owner_id(default_home: Path) -> None:
    created_calendar = json.loads(calendar_create_calendar({"name": "Work"}))
    assert created_calendar["ok"] is True
    assert created_calendar["calendar"]["owner_profile_id"] == "default"
    calendar_id = created_calendar["calendar"]["id"]

    created_event = json.loads(
        calendar_create_event(
            {
                "calendar_id": calendar_id,
                "summary": "Standup",
                "start": "2026-04-01T09:00:00Z",
                "end": "2026-04-01T09:15:00Z",
                "timezone": "UTC",
            }
        )
    )
    assert created_event["ok"] is True
    assert created_event["event"]["owner_profile_id"] == "default"

    fetched = json.loads(calendar_get_event({"event_id": created_event["event"]["id"]}))
    assert fetched["event"]["owner_profile_id"] == "default"

    listed = json.loads(calendar_list_calendars({}))
    assert {row["owner_profile_id"] for row in listed["calendars"]} == {"default"}


def test_two_profiles_see_each_other_in_the_shared_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert current_profile_id() == "default"
    first = verify_shared(PLUGIN, current_profile_id())
    assert first.ok is True
    assert first.peers_seen == ()

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coding-style"))
    assert current_profile_id() == "coding-style"
    named = verify_shared(PLUGIN, current_profile_id())
    assert named.ok is True
    assert "default" in named.peers_seen

    monkeypatch.setenv("HERMES_HOME", str(root))
    default = verify_shared(PLUGIN, current_profile_id())
    assert default.ok is True
    assert "coding-style" in default.peers_seen


def test_default_profile_creates_a_calendar_and_a_global_event(default_home: Path) -> None:
    service = CalendarService()

    calendar = service.create_calendar(name="Default Work")
    assert calendar["calendar"]["owner_profile_id"] == "default"

    global_calendars = service.list_calendars(scope="global")
    assert global_calendars["count"] == 1
    global_calendar_id = global_calendars["calendars"][0]["id"]

    created = service.create_event(
        scope="global",
        calendar_id=global_calendar_id,
        summary="Shared planning",
        start="2026-04-02T09:00:00Z",
        end="2026-04-02T10:00:00Z",
        timezone="UTC",
    )
    assert created["event"]["owner_profile_id"] == "default"

    listed = service.list_events(
        scope="global",
        calendar_id=global_calendar_id,
        start="2026-04-02T00:00:00Z",
        end="2026-04-03T00:00:00Z",
        timezone="UTC",
    )
    assert listed["count"] == 1
    assert listed["events"][0]["owner_profile_id"] == "default"
