"""Tests for the Hermes calendar tool layer.

The Hermes host is not installed, so ``register`` is driven with a fake context
and the stores are redirected through ``HERMES_HOME``. Handlers are called
exactly as the Hermes dispatcher calls them: ``handler(args)`` returning JSON.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from hermes_calendar.db import Repository, StoreScopeError, open_profile_store
from hermes_calendar.hermes import tools
from hermes_calendar.models import EventDraft
from hermes_calendar.plugin import register

PROFILE = "work"
TOOL_NAMES = {
    "calendar_status",
    "calendar_list_calendars",
    "calendar_create_calendar",
    "calendar_create_event",
    "calendar_get_event",
    "calendar_update_event",
    "calendar_delete_event",
    "calendar_list_events",
    "calendar_import_ics",
    "calendar_export_ics",
    "calendar_list_notifications",
    "calendar_dismiss_notification",
    "calendar_snooze_notification",
}
REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeContext:
    """Minimal stand-in for Hermes ``PluginContext``."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


@pytest.fixture
def hermes_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point profile and global resolution at a fresh temporary Hermes root."""
    home = tmp_path / "root" / "profiles" / PROFILE
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    return home


def call(handler: Callable[..., str], args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a tool handler and decode its JSON result."""
    raw = handler(args)
    assert isinstance(raw, str)
    payload: Any = json.loads(raw)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def utc_epoch(value: str) -> int:
    """Epoch seconds for an ISO instant used in assertions."""
    return int(datetime.fromisoformat(value).timestamp())


def create_profile_calendar(name: str = "Work") -> str:
    """Create a profile calendar through the tool and return its ID."""
    payload = call(tools.calendar_create_calendar, {"name": name})
    assert payload["ok"] is True
    return cast(str, payload["calendar"]["id"])


def create_event(calendar_id: str, **overrides: Any) -> dict[str, Any]:
    """Create a standard test event and return the event payload."""
    args: dict[str, Any] = {
        "calendar_id": calendar_id,
        "summary": "Weekly review",
        "start": "2026-03-02T09:00:00",
        "end": "2026-03-02T10:00:00",
        "timezone": "America/New_York",
    }
    args.update(overrides)
    payload = call(tools.calendar_create_event, args)
    assert payload["ok"] is True, payload
    return cast(dict[str, Any], payload["event"])


# --------------------------------------------------------------------- registration


def test_register_registers_every_calendar_tool() -> None:
    ctx = FakeContext()

    register(ctx)

    names = {tool["name"] for tool in ctx.tools}
    assert names == TOOL_NAMES
    for tool in ctx.tools:
        assert tool["toolset"] == "calendar"
        assert callable(tool["handler"])
        assert tool["schema"]["name"] == tool["name"]
        assert tool["schema"]["parameters"]["type"] == "object"


def test_manifest_declares_every_tool() -> None:
    manifest: Any = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    assert set(manifest["provides_tools"]) == TOOL_NAMES


# ----------------------------------------------------------------------- calendars


def test_profile_starts_with_no_calendars(hermes_home: Path) -> None:
    del hermes_home
    payload = call(tools.calendar_list_calendars)

    assert payload == {"ok": True, "scope": "profile", "count": 0, "calendars": []}


def test_create_and_list_profile_calendar(hermes_home: Path) -> None:
    del hermes_home
    created = call(
        tools.calendar_create_calendar,
        {
            "name": "Work",
            "description": "Day job",
            "color": "#336699",
            "default_tz": "Europe/Paris",
            "read_only": True,
        },
    )

    assert created["ok"] is True
    calendar = created["calendar"]
    assert calendar["name"] == "Work"
    assert calendar["description"] == "Day job"
    assert calendar["color"] == "#336699"
    assert calendar["default_tz"] == "Europe/Paris"
    assert calendar["read_only"] is True
    assert calendar["scope"] == "profile"

    listed = call(tools.calendar_list_calendars, {"scope": "profile"})
    assert listed["count"] == 1
    assert listed["calendars"][0]["id"] == calendar["id"]


def test_calendar_defaults_to_profile_timezone(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")
    created = call(tools.calendar_create_calendar, {"name": "Work"})

    assert created["calendar"]["default_tz"] == "Asia/Tokyo"


def test_global_store_has_one_calendar_and_rejects_another(hermes_home: Path) -> None:
    del hermes_home
    listed = call(tools.calendar_list_calendars, {"scope": "global"})
    assert listed["count"] == 1
    assert listed["calendars"][0]["id"] == "cal_global"

    created = call(tools.calendar_create_calendar, {"name": "Second", "scope": "global"})

    assert created["ok"] is False
    assert created["code"] == "conflict"
    assert "single shared calendar" in created["error"]


def test_create_calendar_rejects_bad_arguments(hermes_home: Path) -> None:
    del hermes_home
    assert call(tools.calendar_create_calendar, {"name": ""})["code"] == "invalid_argument"
    assert (
        call(tools.calendar_create_calendar, {"name": "Work", "read_only": "yes"})["code"]
        == "invalid_argument"
    )
    assert (
        call(tools.calendar_create_calendar, {"name": "Work", "default_tz": "Mars/Base"})["code"]
        == "invalid_argument"
    )
    assert (
        call(tools.calendar_create_calendar, {"name": "Work", "default_tz": 5})["code"]
        == "invalid_argument"
    )
    assert (
        call(tools.calendar_create_calendar, {"name": "Work", "description": 5})["code"]
        == "invalid_argument"
    )


def test_unknown_scope_is_rejected(hermes_home: Path) -> None:
    del hermes_home
    payload = call(tools.calendar_list_calendars, {"scope": "shared"})

    assert payload["ok"] is False
    assert payload["code"] == "invalid_argument"
    assert "scope must be one of" in payload["error"]


def test_arguments_must_be_an_object(hermes_home: Path) -> None:
    del hermes_home
    payload = call(tools.calendar_list_calendars, cast(Any, ["not", "an", "object"]))

    assert payload["ok"] is False
    assert payload["code"] == "invalid_argument"


# -------------------------------------------------------------------------- events


@pytest.mark.parametrize("scope", ["profile", "global"])
def test_event_lifecycle_in_both_scopes(hermes_home: Path, scope: str) -> None:
    del hermes_home
    calendar_id = create_profile_calendar() if scope == "profile" else "cal_global"

    event = create_event(calendar_id, scope=scope)
    assert event["summary"] == "Weekly review"
    assert event["start"]["utc"] == "2026-03-02T14:00:00Z"
    assert event["start"]["local"] == "2026-03-02T09:00:00"
    assert event["start"]["tz"] == "America/New_York"
    assert event["duration_seconds"] == 3600
    assert event["scope"] == scope
    assert event["owner_profile_id"] == PROFILE
    event_id = cast(str, event["id"])
    revision = cast(int, event["rev"])

    fetched = call(tools.calendar_get_event, {"scope": scope, "event_id": event_id})
    assert fetched["event"]["id"] == event_id

    updated = call(
        tools.calendar_update_event,
        {
            "scope": scope,
            "event_id": event_id,
            "expected_rev": revision,
            "summary": "Weekly review (moved)",
        },
    )
    assert updated["ok"] is True
    assert updated["event"]["summary"] == "Weekly review (moved)"
    assert updated["event"]["rev"] > revision

    listed = call(
        tools.calendar_list_events,
        {
            "scope": scope,
            "start": "2026-03-02T00:00:00Z",
            "end": "2026-03-03T00:00:00Z",
        },
    )
    assert listed["ok"] is True
    assert listed["recurrence_expanded"] is False
    assert [item["id"] for item in listed["events"]] == [event_id]

    deleted = call(tools.calendar_delete_event, {"scope": scope, "event_id": event_id})
    assert deleted == {
        "ok": True,
        "scope": scope,
        "event_id": event_id,
        "deleted": True,
        "already_deleted": False,
    }

    assert (
        call(tools.calendar_get_event, {"scope": scope, "event_id": event_id})["code"]
        == "not_found"
    )
    after = call(
        tools.calendar_list_events,
        {
            "scope": scope,
            "start": "2026-03-02T00:00:00Z",
            "end": "2026-03-03T00:00:00Z",
        },
    )
    assert after["count"] == 0


def test_naive_datetime_uses_profile_config_timezone(hermes_home: Path) -> None:
    (hermes_home / "config.yaml").write_text("timezone: America/New_York\n", encoding="utf-8")
    calendar_id = create_profile_calendar()

    event = create_event(calendar_id, timezone=None)

    assert event["start"]["utc"] == "2026-03-02T14:00:00Z"
    assert event["start"]["tz"] == "America/New_York"


def test_env_timezone_overrides_config(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (hermes_home / "config.yaml").write_text("timezone: Asia/Tokyo\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    calendar_id = create_profile_calendar()

    event = create_event(calendar_id, timezone=None)

    assert event["start"]["tz"] == "America/New_York"


def test_quoted_config_timezone_is_used(hermes_home: Path) -> None:
    (hermes_home / "config.yaml").write_text('timezone: "Asia/Tokyo"\n', encoding="utf-8")
    calendar_id = create_profile_calendar()

    event = create_event(calendar_id, timezone=None)

    assert event["start"]["tz"] == "Asia/Tokyo"


def test_invalid_config_timezone_falls_back_to_utc(hermes_home: Path) -> None:
    (hermes_home / "config.yaml").write_text('timezone: "Mars/Base"  # bad\n', encoding="utf-8")
    calendar_id = create_profile_calendar()

    event = create_event(calendar_id, timezone=None)

    assert event["start"]["utc"] == "2026-03-02T09:00:00Z"
    assert event["start"]["tz"] == "UTC"


def test_offset_datetime_is_absolute(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()

    event = create_event(
        calendar_id,
        start="2026-03-02T09:00:00-05:00",
        end="2026-03-02T10:00:00-05:00",
        timezone="Asia/Tokyo",
    )

    assert event["start"]["utc"] == "2026-03-02T14:00:00Z"
    assert event["start"]["local"] == "2026-03-02T23:00:00"
    assert event["start"]["tz"] == "Asia/Tokyo"


def test_config_without_timezone_falls_back_to_utc(hermes_home: Path) -> None:
    (hermes_home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    calendar_id = create_profile_calendar()

    event = create_event(calendar_id, timezone=None)

    assert event["start"]["tz"] == "UTC"


def test_create_event_rejects_bad_arguments(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    base: dict[str, Any] = {
        "calendar_id": calendar_id,
        "summary": "Review",
        "start": "2026-03-02T09:00:00Z",
        "end": "2026-03-02T10:00:00Z",
    }

    for overrides, expected in (
        ({"calendar_id": ""}, "calendar_id is required"),
        ({"summary": ""}, "summary is required"),
        ({"start": 5}, "start is required"),
        ({"start": "not-a-date"}, "start must be an ISO 8601"),
        ({"end": "2026-03-02T08:00:00Z"}, "end must be later than start"),
        ({"timezone": "Mars/Base"}, "timezone must be a valid IANA"),
        ({"timezone": 5}, "timezone must be a non-empty"),
        ({"timezone": ""}, "timezone must be a non-empty"),
        ({"description": 5}, "description must be a string"),
        ({"description": "x" * (tools._MAX_TEXT + 1)}, "description must be at most"),
    ):
        args = dict(base)
        args.update(overrides)
        payload = call(tools.calendar_create_event, args)
        assert payload["ok"] is False, overrides
        assert expected in payload["error"], overrides

    assert (
        call(tools.calendar_create_event, {**base, "calendar_id": "cal_missing"})["code"]
        == "not_found"
    )


def test_create_event_enforces_text_limit(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()

    payload = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "x" * (tools._MAX_TEXT + 1),
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T10:00:00Z",
        },
    )

    assert payload["code"] == "invalid_argument"
    assert "summary must be at most" in payload["error"]


def test_get_missing_event_is_not_found(hermes_home: Path) -> None:
    del hermes_home
    payload = call(tools.calendar_get_event, {"event_id": "missing"})

    assert payload["ok"] is False
    assert payload["code"] == "not_found"
    assert payload["error_type"] == "EventNotFoundError"


# ------------------------------------------------------------------------- update


def test_update_only_start_preserves_duration(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)

    updated = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "start": "2026-03-02T10:00:00",
        },
    )

    assert updated["ok"] is True
    assert updated["event"]["start"]["local"] == "2026-03-02T10:00:00"
    assert updated["event"]["start"]["utc"] == "2026-03-02T15:00:00Z"
    assert updated["event"]["end"]["utc"] == "2026-03-02T16:00:00Z"
    assert updated["event"]["duration_seconds"] == 3600


def test_update_only_end_changes_duration(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)

    updated = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "end": "2026-03-02T16:00:00Z",
            "timezone": "UTC",
        },
    )

    assert updated["event"]["start"]["utc"] == "2026-03-02T14:00:00Z"
    assert updated["event"]["start"]["tz"] == "UTC"
    assert updated["event"]["duration_seconds"] == 7200


def test_update_clears_optional_fields(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id, description="draft", location="Room 4B", url="https://x")

    updated = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "description": None,
            "location": None,
            "url": None,
        },
    )

    assert updated["event"]["description"] is None
    assert updated["event"]["location"] is None
    assert updated["event"]["url"] is None


def test_update_rejects_bad_arguments(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)

    cases = (
        ({"expected_rev": event["rev"]}, "provide at least one field"),
        ({"expected_rev": 0, "summary": "x"}, "expected_rev must be at least 1"),
        ({"expected_rev": True, "summary": "x"}, "expected_rev is required and must be an integer"),
        ({"expected_rev": event["rev"], "summary": " "}, "summary must be a non-empty"),
        (
            {"expected_rev": event["rev"], "summary": "x" * (tools._MAX_TEXT + 1)},
            "summary must be at most",
        ),
        ({"expected_rev": event["rev"], "description": 5}, "description must be a string"),
        (
            {
                "expected_rev": event["rev"],
                "start": "2026-03-02T12:00:00Z",
                "end": "2026-03-02T11:00:00Z",
            },
            "end must be later than start",
        ),
    )
    for overrides, expected in cases:
        payload = call(
            tools.calendar_update_event,
            {"event_id": event["id"], **overrides},
        )
        assert payload["code"] == "invalid_argument", overrides
        assert expected in payload["error"], overrides

    missing = call(
        tools.calendar_update_event,
        {"event_id": "missing", "expected_rev": 1, "summary": "x"},
    )
    assert missing["code"] == "not_found"


def test_update_conflict_is_structured(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)
    first = call(
        tools.calendar_update_event,
        {"event_id": event["id"], "expected_rev": event["rev"], "summary": "First"},
    )
    assert first["ok"] is True

    stale = call(
        tools.calendar_update_event,
        {"event_id": event["id"], "expected_rev": event["rev"], "summary": "Second"},
    )

    assert stale["ok"] is False
    assert stale["code"] == "revision_conflict"
    assert stale["event_id"] == event["id"]
    assert stale["expected_rev"] == event["rev"]

    fetched = call(tools.calendar_get_event, {"event_id": event["id"]})
    assert fetched["event"]["summary"] == "First"


def test_update_deleted_event_is_not_found(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)
    call(tools.calendar_delete_event, {"event_id": event["id"]})

    payload = call(
        tools.calendar_update_event,
        {"event_id": event["id"], "expected_rev": 1, "summary": "x"},
    )

    assert payload["code"] == "not_found"


# ------------------------------------------------------------------------- delete


def test_delete_is_idempotent(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)

    first = call(tools.calendar_delete_event, {"event_id": event["id"]})
    second = call(tools.calendar_delete_event, {"event_id": event["id"]})

    assert first["already_deleted"] is False
    assert second["ok"] is True
    assert second["already_deleted"] is True
    assert call(tools.calendar_delete_event, {"event_id": "missing"})["code"] == "not_found"
    assert (
        call(tools.calendar_delete_event, {"event_id": event["id"], "expected_rev": True})["code"]
        == "invalid_argument"
    )


def test_delete_with_stale_revision_conflicts(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_profile_calendar()
    event = create_event(calendar_id)
    call(
        tools.calendar_update_event,
        {"event_id": event["id"], "expected_rev": event["rev"], "summary": "First"},
    )

    payload = call(
        tools.calendar_delete_event,
        {"event_id": event["id"], "expected_rev": event["rev"]},
    )

    assert payload["ok"] is False
    assert payload["code"] == "revision_conflict"


# --------------------------------------------------------------------------- list


def test_list_events_merges_calendars_and_expands_recurring(hermes_home: Path) -> None:
    del hermes_home
    work = create_profile_calendar("Work")
    home = create_profile_calendar("Home")
    create_event(work, summary="Work review")
    create_event(
        home,
        summary="Home review",
        start="2026-03-02T08:00:00",
        end="2026-03-02T08:30:00",
    )
    connection = open_profile_store()
    try:
        repository = Repository(connection)
        repository.create_event(
            EventDraft(
                calendar_id=work,
                summary="Recurring series",
                start_utc=utc_epoch("2026-03-02T14:00:00+00:00"),
                end_utc=utc_epoch("2026-03-02T15:00:00+00:00"),
                start_local="2026-03-02T09:00:00",
                start_tz="America/New_York",
                duration_seconds=3600,
                rrule="FREQ=WEEKLY;COUNT=3",
            )
        )
    finally:
        connection.close()

    payload = call(
        tools.calendar_list_events,
        {"start": "2026-03-02T00:00:00Z", "end": "2026-03-03T00:00:00Z"},
    )

    assert payload["count"] == 3
    assert [item["summary"] for item in payload["events"]] == [
        "Home review",
        "Work review",
        "Recurring series",
    ]
    assert payload["events"][0]["summary"] == "Home review"
    recurring = payload["events"][2]
    assert recurring["master_id"] == recurring["id"]
    assert recurring["is_override"] is False
    assert recurring["occurrence_start"]["utc"] == "2026-03-02T14:00:00Z"
    assert recurring["recurrence_key"].startswith("T:")
    assert payload["recurrence_expanded"] is True


def test_list_events_filters_one_calendar(hermes_home: Path) -> None:
    del hermes_home
    work = create_profile_calendar("Work")
    home = create_profile_calendar("Home")
    create_event(work)
    create_event(home, summary="Home", start="2026-03-02T08:00:00", end="2026-03-02T08:30:00")

    payload = call(
        tools.calendar_list_events,
        {
            "calendar_id": work,
            "start": "2026-03-02T00:00:00Z",
            "end": "2026-03-03T00:00:00Z",
        },
    )

    assert payload["count"] == 1
    assert payload["events"][0]["calendar_id"] == work


def test_list_events_rejects_bad_arguments(hermes_home: Path) -> None:
    del hermes_home
    base = {"start": "2026-03-02T00:00:00Z", "end": "2026-03-03T00:00:00Z"}

    assert (
        call(tools.calendar_list_events, {**base, "calendar_id": "cal_missing"})["code"]
        == "not_found"
    )
    assert (
        call(tools.calendar_list_events, {**base, "calendar_id": 5})["code"] == "invalid_argument"
    )
    assert (
        call(
            tools.calendar_list_events,
            {"start": "2026-03-03T00:00:00Z", "end": "2026-03-02T00:00:00Z"},
        )["code"]
        == "invalid_argument"
    )


# ------------------------------------------------------------------ failure mapping


def test_store_failure_maps_to_store_error(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del hermes_home

    def fail() -> sqlite3.Connection:
        raise StoreScopeError("wrong store")

    monkeypatch.setattr(tools, "open_profile_store", fail)

    payload = call(tools.calendar_list_calendars)

    assert payload["code"] == "store_error"
    assert payload["error"] == "wrong store"


def test_integrity_error_maps_to_conflict(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del hermes_home

    def fail() -> sqlite3.Connection:
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(tools, "open_profile_store", fail)

    payload = call(tools.calendar_list_calendars)

    assert payload["code"] == "conflict"
    assert "database constraint" in payload["error"]


def test_unexpected_error_never_escapes(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del hermes_home

    def fail() -> sqlite3.Connection:
        raise RuntimeError("boom")

    monkeypatch.setattr(tools, "open_profile_store", fail)

    payload = call(tools.calendar_list_calendars)

    assert payload["ok"] is False
    assert payload["code"] == "internal_error"
    assert "RuntimeError: boom" in payload["error"]
