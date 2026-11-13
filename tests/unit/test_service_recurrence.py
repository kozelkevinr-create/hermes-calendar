"""End-to-end recurrence behavior through the service and tool adapters.

The tests drive ``CalendarService`` through the Hermes handlers exactly as the
dispatcher does, so they cover store selection, validation, expansion, DST, and
occurrence edits in one path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_calendar.hermes import tools
from hermes_calendar.service import CalendarService

PROFILE = "work"


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


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def calendar_for(scope: str) -> str:
    """Return the one shared global calendar or create a profile calendar."""
    if scope == "global":
        return "cal_global"
    payload = call(tools.calendar_create_calendar, {"name": "Work"})
    assert payload["ok"] is True
    return cast(str, payload["calendar"]["id"])


def create_recurring(calendar_id: str, scope: str) -> dict[str, Any]:
    payload = call(
        tools.calendar_create_event,
        {
            "scope": scope,
            "calendar_id": calendar_id,
            "summary": "Standup",
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T09:30:00",
            "timezone": "America/New_York",
            "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=4",
        },
    )
    assert payload["ok"] is True, payload
    return cast(dict[str, Any], payload["event"])


def list_between(scope: str, calendar_id: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "scope": scope,
        "start": "2026-03-01T00:00:00Z",
        "end": "2026-04-01T00:00:00Z",
    }
    if calendar_id is not None:
        args["calendar_id"] = calendar_id
    return call(tools.calendar_list_events, args)


# ------------------------------------------------------------------ DST series


@pytest.mark.parametrize("scope", ["profile", "global"])
def test_recurring_series_across_spring_forward_in_both_scopes(
    hermes_home: Path, scope: str
) -> None:
    del hermes_home
    calendar_id = calendar_for(scope)
    event = create_recurring(calendar_id, scope)
    event_id = cast(str, event["id"])

    payload = list_between(scope, calendar_id)

    assert payload["ok"] is True
    assert payload["recurrence_expanded"] is True
    assert payload["count"] == 4
    starts = [item["occurrence_start"]["utc"] for item in payload["events"]]
    assert starts == [
        "2026-03-02T14:00:00Z",
        "2026-03-09T13:00:00Z",
        "2026-03-16T13:00:00Z",
        "2026-03-23T13:00:00Z",
    ]
    assert [item["occurrence_start"]["local"] for item in payload["events"]] == [
        "2026-03-02T09:00:00",
        "2026-03-09T09:00:00",
        "2026-03-16T09:00:00",
        "2026-03-23T09:00:00",
    ]
    assert [item["occurrence_start"]["tz"] for item in payload["events"]] == [
        "America/New_York"
    ] * 4
    assert {item["master_id"] for item in payload["events"]} == {event_id}
    assert all(item["is_override"] is False for item in payload["events"])
    assert payload["events"][1]["recurrence_key"] == f"T:{epoch('2026-03-09T13:00:00Z')}"


@pytest.mark.parametrize("scope", ["profile", "global"])
def test_occurrence_edit_exdate_and_delete_in_both_scopes(hermes_home: Path, scope: str) -> None:
    del hermes_home
    calendar_id = calendar_for(scope)
    event = create_recurring(calendar_id, scope)
    event_id = cast(str, event["id"])

    with_exdate = call(
        tools.calendar_update_event,
        {
            "scope": scope,
            "event_id": event_id,
            "expected_rev": event["rev"],
            "exdates": ["2026-03-16T09:00:00"],
        },
    )
    assert with_exdate["ok"] is True
    assert with_exdate["event"]["exdate"] == ["2026-03-16T09:00:00"]
    assert list_between(scope, calendar_id)["count"] == 3

    moved = call(
        tools.calendar_update_event,
        {
            "scope": scope,
            "event_id": event_id,
            "expected_rev": with_exdate["event"]["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "series_scope": "this",
            "summary": "Standup moved",
            "start": "2026-03-09T10:00:00",
            "end": "2026-03-09T10:30:00",
        },
    )
    assert moved["ok"] is True, moved
    assert moved["event"]["parent_id"] == event_id
    assert moved["event"]["recurrence_key"] == f"T:{epoch('2026-03-09T13:00:00Z')}"

    after_move = list_between(scope, calendar_id)
    overrides = [item for item in after_move["events"] if item["is_override"]]
    assert len(overrides) == 1
    override = overrides[0]
    assert override["summary"] == "Standup moved"
    assert override["master_id"] == event_id
    assert override["override_event_id"] == moved["event"]["id"]
    assert override["occurrence_start"]["utc"] == "2026-03-09T14:00:00Z"
    assert override["occurrence_start"]["local"] == "2026-03-09T10:00:00"
    assert override["recurrence_key"] == f"T:{epoch('2026-03-09T13:00:00Z')}"

    deleted = call(
        tools.calendar_delete_event,
        {
            "scope": scope,
            "event_id": event_id,
            "occurrence_start": "2026-03-23T09:00:00",
        },
    )
    assert deleted["ok"] is True
    assert deleted["deleted"] is True
    assert deleted["recurrence_key"] == f"T:{epoch('2026-03-23T13:00:00Z')}"
    assert list_between(scope, calendar_id)["count"] == 2

    series = call(
        tools.calendar_delete_event,
        {"scope": scope, "event_id": event_id, "series_scope": "all"},
    )
    assert series["ok"] is True
    assert series["deleted_overrides"] == 2
    assert list_between(scope, calendar_id)["count"] == 0


# ------------------------------------------------------------ series arguments


def test_create_accepts_rdates_exdates_and_unsupported_rules_are_structured(
    hermes_home: Path,
) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")

    unsupported = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "Hourly",
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T10:00:00Z",
            "rrule": "FREQ=HOURLY",
        },
    )
    assert unsupported["ok"] is False
    assert unsupported["code"] == "unsupported_recurrence"
    assert "FREQ=HOURLY" in unsupported["error"]

    malformed = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "Broken",
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T10:00:00Z",
            "rrule": "BYDAY=MO",
        },
    )
    assert malformed["code"] == "invalid_argument"

    created = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "RDATE series",
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T09:30:00Z",
            "rdates": ["2026-03-05T09:00:00", "2026-03-06T09:00:00"],
            "exdates": ["2026-03-06T09:00:00"],
        },
    )
    assert created["ok"] is True
    assert created["event"]["rdate"] == [
        "2026-03-05T09:00:00",
        "2026-03-06T09:00:00",
    ]
    assert created["event"]["exdate"] == ["2026-03-06T09:00:00"]

    listed = list_between("profile", calendar_id)
    assert listed["count"] == 2
    assert [item["occurrence_start"]["utc"] for item in listed["events"]] == [
        "2026-03-02T09:00:00Z",
        "2026-03-05T09:00:00Z",
    ]


def test_update_series_can_change_and_clear_rrule(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")

    changed = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=2",
        },
    )
    assert changed["ok"] is True
    assert changed["event"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO;COUNT=2"
    assert list_between("profile", calendar_id)["count"] == 2

    cleared = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": changed["event"]["rev"],
            "rrule": None,
        },
    )
    assert cleared["ok"] is True
    assert cleared["event"]["rrule"] is None
    listed = list_between("profile", calendar_id)
    assert listed["recurrence_expanded"] is False
    assert listed["count"] == 1


def test_update_occurrence_keeps_series_when_scope_all_is_explicit(
    hermes_home: Path,
) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")

    updated = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "series_scope": "all",
            "summary": "Whole series renamed",
        },
    )

    assert updated["ok"] is True
    assert updated["event"]["summary"] == "Whole series renamed"
    assert updated["event"]["parent_id"] is None
    listed = list_between("profile", calendar_id)
    assert all(item["summary"] == "Whole series renamed" for item in listed["events"])
    assert all(item["is_override"] is False for item in listed["events"])


def test_series_argument_validation(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")

    cases = (
        (
            {
                "event_id": event["id"],
                "expected_rev": event["rev"],
                "series_scope": "this",
                "summary": "x",
            },
            "occurrence_start is required",
        ),
        (
            {
                "event_id": event["id"],
                "expected_rev": event["rev"],
                "series_scope": "future",
                "summary": "x",
            },
            "series_scope must be",
        ),
        (
            {
                "event_id": event["id"],
                "expected_rev": event["rev"],
                "occurrence_start": "2026-03-10T09:00:00",
                "summary": "x",
            },
            "occurrence_start is not an occurrence",
        ),
        (
            {
                "event_id": event["id"],
                "expected_rev": event["rev"],
                "occurrence_start": "2026-03-09T09:00:00",
                "rrule": "FREQ=DAILY",
            },
            "cannot be changed for a single occurrence",
        ),
        (
            {
                "event_id": event["id"],
                "expected_rev": event["rev"],
                "rdates": 5,
            },
            "rdates must be an array",
        ),
        (
            {
                "event_id": event["id"],
                "expected_rev": event["rev"],
                "exdates": ["not-a-date"],
            },
            "malformed recurrence",
        ),
    )
    for args, expected in cases:
        payload = call(tools.calendar_update_event, args)
        assert payload["code"] == "invalid_argument", args
        assert expected in payload["error"], args

    stale = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"] + 1,
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "stale",
        },
    )
    assert stale["code"] == "revision_conflict"

    delete_without_occurrence = call(
        tools.calendar_delete_event,
        {"event_id": event["id"], "series_scope": "this"},
    )
    assert delete_without_occurrence["code"] == "invalid_argument"


def test_unsupported_series_update_is_rejected(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")

    payload = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "rrule": "FREQ=WEEKLY;BYHOUR=9",
        },
    )

    assert payload["ok"] is False
    assert payload["code"] == "unsupported_recurrence"
    assert "BYHOUR" in payload["error"]


# --------------------------------------------------------------------- service


def test_service_opens_the_default_profile_and_global_stores(hermes_home: Path) -> None:
    del hermes_home
    service = CalendarService()

    profile = service.list_calendars()
    global_store = service.list_calendars(scope="global")

    assert profile["scope"] == "profile"
    assert profile["count"] == 1
    assert profile["calendars"][0]["name"] == "Personal"
    assert global_store["scope"] == "global"
    assert global_store["calendars"][0]["id"] == "cal_global"


def test_list_rejects_unknown_calendar_and_bad_range(hermes_home: Path) -> None:
    del hermes_home
    assert (
        call(
            tools.calendar_list_events,
            {
                "start": "2026-03-01T00:00:00Z",
                "end": "2026-03-02T00:00:00Z",
                "calendar_id": "cal_missing",
            },
        )["code"]
        == "not_found"
    )
    assert (
        call(
            tools.calendar_list_events,
            {"start": "2026-03-02T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
        )["code"]
        == "invalid_argument"
    )


# ------------------------------------------------------- more series edit paths


def test_occurrence_edit_updates_an_existing_override(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")

    first = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "First edit",
            "description": "notes",
            "location": "Room 1",
            "url": "https://example.test/1",
            "status": "TENTATIVE",
        },
    )
    assert first["ok"] is True
    assert first["event"]["description"] == "notes"
    assert first["event"]["location"] == "Room 1"
    assert first["event"]["url"] == "https://example.test/1"
    assert first["event"]["status"] == "TENTATIVE"

    second = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "Second edit",
        },
    )
    assert second["ok"] is True
    assert second["event"]["id"] == first["event"]["id"]
    assert second["event"]["summary"] == "Second edit"
    assert second["event"]["rev"] > first["event"]["rev"]

    noop = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
        },
    )
    assert noop["code"] == "invalid_argument"
    assert "provide at least one field" in noop["error"]


def test_delete_occurrence_twice_is_idempotent(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")

    first = call(
        tools.calendar_delete_event,
        {
            "event_id": event["id"],
            "occurrence_start": "2026-03-09T09:00:00",
        },
    )
    second = call(
        tools.calendar_delete_event,
        {
            "event_id": event["id"],
            "occurrence_start": "2026-03-09T09:00:00",
        },
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["override_event_id"] == second["override_event_id"]
    assert list_between("profile", calendar_id)["count"] == 3


def test_series_edit_can_address_an_override_and_delete_series_with_overrides(
    hermes_home: Path,
) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")
    override = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "Moved instance",
        },
    )
    assert override["ok"] is True

    renamed = call(
        tools.calendar_update_event,
        {
            "event_id": override["event"]["id"],
            "expected_rev": event["rev"],
            "series_scope": "all",
            "summary": "Series renamed",
        },
    )
    assert renamed["ok"] is True
    assert renamed["event"]["id"] == event["id"]
    assert renamed["event"]["summary"] == "Series renamed"

    stale_delete = call(
        tools.calendar_delete_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"] + 99,
            "series_scope": "all",
        },
    )
    assert stale_delete["code"] == "revision_conflict"
    assert list_between("profile", calendar_id)["count"] == 4

    deleted = call(
        tools.calendar_delete_event,
        {
            "event_id": event["id"],
            "expected_rev": renamed["event"]["rev"],
            "series_scope": "all",
        },
    )
    assert deleted["ok"] is True
    assert deleted["deleted_overrides"] == 1
    assert list_between("profile", calendar_id)["count"] == 0


def test_rrule_and_recurrence_value_normalization(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    created = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "Prefixed",
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T10:00:00Z",
            "rrule": "RRULE:FREQ=DAILY;COUNT=2",
        },
    )
    assert created["ok"] is True
    assert created["event"]["rrule"] == "FREQ=DAILY;COUNT=2"

    for rrule, expected in (
        ("", "rrule must be a non-empty"),
        ("   ", "rrule must be a non-empty"),
    ):
        bad = call(
            tools.calendar_create_event,
            {
                "calendar_id": calendar_id,
                "summary": "Bad",
                "start": "2026-03-02T09:00:00Z",
                "end": "2026-03-02T10:00:00Z",
                "rrule": rrule,
            },
        )
        assert bad["code"] == "invalid_argument"
        assert expected in bad["error"]

    for value in ([5], ["/"], ["not-a-date"]):
        bad_values = call(
            tools.calendar_create_event,
            {
                "calendar_id": calendar_id,
                "summary": "Bad values",
                "start": "2026-03-02T09:00:00Z",
                "end": "2026-03-02T10:00:00Z",
                "rdates": value,
            },
        )
        assert bad_values["code"] == "invalid_argument", value

    updated = call(
        tools.calendar_update_event,
        {
            "event_id": created["event"]["id"],
            "expected_rev": created["event"]["rev"],
            "exdates": [
                "2026-03-16T09:00:00-04:00",
                "20260323T090000Z",
                "20260330T090000",
            ],
        },
    )
    assert updated["ok"] is True
    assert updated["event"]["exdate"] == [
        "2026-03-16T13:00:00Z",
        "2026-03-23T09:00:00Z",
        "2026-03-30T09:00:00",
    ]


def test_delete_occurrence_updates_an_existing_override(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")
    moved = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "Moved first",
        },
    )
    assert moved["ok"] is True

    deleted = call(
        tools.calendar_delete_event,
        {
            "event_id": event["id"],
            "occurrence_start": "2026-03-09T09:00:00",
        },
    )

    assert deleted["ok"] is True
    assert deleted["override_event_id"] == moved["event"]["id"]
    assert list_between("profile", calendar_id)["count"] == 3


def test_occurrence_lookup_skips_an_earlier_moved_override(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")
    moved_first = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-02T09:00:00",
            "summary": "March 2 moved to March 9",
            "start": "2026-03-09T09:00:00",
            "end": "2026-03-09T09:30:00",
        },
    )
    assert moved_first["ok"] is True

    target = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "March 9 renamed",
        },
    )

    assert target["ok"] is True
    assert target["event"]["recurrence_key"] == f"T:{epoch('2026-03-09T13:00:00Z')}"


def test_list_expands_utc_exdates(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")
    updated = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "exdates": ["2026-03-16T13:00:00Z", "2026-03-23T13:00:00Z"],
        },
    )
    assert updated["ok"] is True

    listed = list_between("profile", calendar_id)

    assert listed["count"] == 2
    assert [item["occurrence_start"]["local"] for item in listed["events"]] == [
        "2026-03-02T09:00:00",
        "2026-03-09T09:00:00",
    ]


def test_malformed_stored_timing_is_reported_not_raised(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    from hermes_calendar.db import Repository, open_profile_store
    from hermes_calendar.models import EventDraft

    connection = open_profile_store()
    try:
        Repository(connection).create_event(
            EventDraft(
                calendar_id=calendar_id,
                summary="Bad timing",
                start_utc=epoch("2026-03-02T09:00:00Z"),
                end_utc=epoch("2026-03-02T10:00:00Z"),
                start_local="not-a-local-time",
                start_tz="UTC",
                duration_seconds=3600,
                rrule="FREQ=DAILY;COUNT=2",
            )
        )
    finally:
        connection.close()

    payload = list_between("profile", calendar_id)

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["recurrence_warnings"]


def test_deleting_an_occurrence_of_a_deleted_series_is_not_found(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")
    assert (
        call(
            tools.calendar_delete_event,
            {"event_id": event["id"], "series_scope": "all"},
        )["ok"]
        is True
    )

    payload = call(
        tools.calendar_delete_event,
        {"event_id": event["id"], "occurrence_start": "2026-03-09T09:00:00"},
    )

    assert payload["code"] == "not_found"


def test_editing_an_override_under_a_deleted_master_is_not_found(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    event = create_recurring(calendar_id, "profile")
    override = call(
        tools.calendar_update_event,
        {
            "event_id": event["id"],
            "expected_rev": event["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "summary": "Moved",
        },
    )
    assert override["ok"] is True
    from hermes_calendar.db import Repository, open_profile_store

    connection = open_profile_store()
    try:
        Repository(connection).delete_event(cast(str, event["id"]))
    finally:
        connection.close()

    payload = call(
        tools.calendar_update_event,
        {
            "event_id": override["event"]["id"],
            "expected_rev": override["event"]["rev"],
            "summary": "Orphan edit",
        },
    )

    assert payload["code"] == "not_found"


def test_string_recurrence_values_are_accepted(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")

    payload = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "RDATE string",
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T10:00:00Z",
            "rdates": "2026-03-05T09:00:00",
        },
    )

    assert payload["ok"] is True
    assert payload["event"]["rdate"] == ["2026-03-05T09:00:00"]


def test_malformed_series_degrades_to_dtstart_with_a_warning(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    from hermes_calendar.db import Repository, open_profile_store
    from hermes_calendar.models import EventDraft

    connection = open_profile_store()
    try:
        Repository(connection).create_event(
            EventDraft(
                calendar_id=calendar_id,
                summary="Broken rule",
                start_utc=epoch("2026-03-02T09:00:00Z"),
                end_utc=epoch("2026-03-02T10:00:00Z"),
                start_local="2026-03-02T09:00:00",
                start_tz="UTC",
                duration_seconds=3600,
                rrule="BYDAY=MO",
            )
        )
    finally:
        connection.close()

    payload = list_between("profile", calendar_id)

    assert payload["count"] == 1
    assert payload["events"][0]["occurrence_start"]["utc"] == "2026-03-02T09:00:00Z"
    assert payload["recurrence_warnings"]


def test_all_day_series_edits_and_occurrence_overrides(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:allday-edit@example\r\n"
        "SUMMARY:Holiday\r\n"
        "DTSTART;VALUE=DATE:20260302\r\n"
        "DTEND;VALUE=DATE:20260303\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=5\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    assert (
        call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": ics})["imported"] == 1
    )
    master = list_between("profile", calendar_id)["events"][0]

    compact = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": master["rev"],
            "exdates": ["20260316"],
        },
    )
    assert compact["ok"] is True
    assert compact["event"]["exdate"] == ["2026-03-16"]

    date_time = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": compact["event"]["rev"],
            "exdates": ["20260316", "2026-03-23T00:00:00Z"],
        },
    )
    assert date_time["ok"] is True
    assert date_time["event"]["exdate"] == ["2026-03-16", "2026-03-23"]

    for bad_date in ("not-a-date", "2026-13-45"):
        invalid = call(
            tools.calendar_update_event,
            {
                "event_id": master["master_id"],
                "expected_rev": date_time["event"]["rev"],
                "exdates": [bad_date],
            },
        )
        assert invalid["code"] == "invalid_argument"

    override = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": date_time["event"]["rev"],
            "occurrence_start": "2026-03-09",
            "summary": "Holiday renamed",
        },
    )
    assert override["ok"] is True
    assert override["event"]["recurrence_id_raw"] == "2026-03-09"

    switched = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": date_time["event"]["rev"],
            "occurrence_start": "2026-03-09",
            "status": "CANCELLED",
        },
    )
    assert switched["ok"] is True
    assert switched["event"]["status"] == "CANCELLED"

    listed = list_between("profile", calendar_id)
    assert listed["count"] == 2
    assert [item["occurrence_start"]["date"] for item in listed["events"]] == [
        "2026-03-02",
        "2026-03-30",
    ]

    bad_series_time = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": date_time["event"]["rev"],
            "start": "2026-03-02T09:00:00Z",
        },
    )
    assert bad_series_time["code"] == "invalid_argument"

    bad_occurrence_time = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": date_time["event"]["rev"],
            "occurrence_start": 5,
        },
    )
    assert bad_occurrence_time["code"] == "invalid_argument"

    bad_occurrence_date = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": date_time["event"]["rev"],
            "occurrence_start": "not-a-date",
        },
    )
    assert bad_occurrence_date["code"] == "invalid_argument"

    bad_status = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": date_time["event"]["rev"],
            "occurrence_start": "2026-03-09",
            "status": "MAYBE",
        },
    )
    assert bad_status["code"] == "invalid_argument"


def test_service_creates_all_day_events_with_exclusive_end_dates(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    service = CalendarService()

    created = service.create_event(
        calendar_id=calendar_id,
        summary="Company holiday",
        start="2026-03-02",
        end="2026-03-03",
        all_day=True,
    )

    event = created["event"]
    assert event["all_day"] is True
    assert event["start"]["date"] == "2026-03-02"
    assert event["end"]["date"] == "2026-03-03"
    assert event["duration_seconds"] is None
    assert event["start"]["utc"] == "2026-03-02T00:00:00Z"

    listed = service.list_events(
        scope="profile",
        start="2026-03-01T00:00:00Z",
        end="2026-04-01T00:00:00Z",
        calendar_id=calendar_id,
    )
    assert listed["count"] == 1
    assert listed["events"][0]["occurrence_start"]["date"] == "2026-03-02"


def test_service_accepts_datetime_input_for_all_day_boundaries(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")

    created = CalendarService().create_event(
        calendar_id=calendar_id,
        summary="Imported holiday",
        start="2026-03-02T00:00:00Z",
        end="20260303",
        all_day=True,
    )

    assert created["event"]["start"]["date"] == "2026-03-02"
    assert created["event"]["end"]["date"] == "2026-03-03"


def test_service_creates_recurring_all_day_events(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")

    created = CalendarService().create_event(
        calendar_id=calendar_id,
        summary="Weekly holiday",
        start="2026-03-02",
        end="2026-03-03",
        all_day=True,
        rrule="FREQ=WEEKLY;COUNT=3",
    )

    listed = CalendarService().list_events(
        scope="profile",
        start="2026-03-01T00:00:00Z",
        end="2026-04-01T00:00:00Z",
        calendar_id=calendar_id,
    )

    assert created["event"]["all_day"] is True
    assert listed["count"] == 3
    assert {item["occurrence_start"]["date"] for item in listed["events"]} == {
        "2026-03-02",
        "2026-03-09",
        "2026-03-16",
    }


def test_all_day_creation_validation(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = calendar_for("profile")
    service = CalendarService()

    def create(**overrides: Any) -> str:
        arguments: dict[str, Any] = {
            "calendar_id": calendar_id,
            "summary": "Holiday",
            "start": "2026-03-02",
            "end": "2026-03-03",
            "all_day": True,
        }
        arguments.update(overrides)
        try:
            service.create_event(**arguments)
        except Exception as exc:  # noqa: BLE001 - asserting the public error code
            return str(getattr(exc, "code", type(exc).__name__))
        return "ok"

    assert create() == "ok"
    assert create(end="2026-03-02") == "invalid_argument"
    assert create(end="2026-03-01") == "invalid_argument"
    assert create(start="") == "invalid_argument"
    assert create(start=7) == "invalid_argument"
    assert create(start="not-a-date") == "invalid_argument"
    assert create(start="2026-13-45") == "invalid_argument"
    assert create(all_day="yes") == "invalid_argument"
    assert create(timezone="Mars/Base") == "invalid_argument"
    assert create(timezone=5) == "invalid_argument"
