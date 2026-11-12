"""End-to-end iCalendar import/export through the Hermes tool adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_calendar import service
from hermes_calendar.hermes import tools

PROFILE = "work"

SERIES_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:series-1@example\r\n"
    "SUMMARY:Weekly\r\n"
    "DTSTART;TZID=America/New_York:20260302T090000\r\n"
    "DTEND;TZID=America/New_York:20260302T093000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
    "EXDATE;TZID=America/New_York:20260316T090000\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:series-1@example\r\n"
    "RECURRENCE-ID;TZID=America/New_York:20260309T090000\r\n"
    "SUMMARY:Weekly moved\r\n"
    "DTSTART;TZID=America/New_York:20260309T100000\r\n"
    "DTEND;TZID=America/New_York:20260309T103000\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:single-1@example\r\n"
    "SUMMARY:One off\r\n"
    "DTSTART:20260305T120000Z\r\n"
    "DTEND:20260305T130000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture
def hermes_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "root" / "profiles" / PROFILE
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    return home


def call(handler: Callable[..., str], args: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = handler(args)
    assert isinstance(raw, str)
    payload: Any = json.loads(raw)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def create_calendar(name: str = "Work") -> str:
    payload = call(tools.calendar_create_calendar, {"name": name})
    assert payload["ok"] is True
    return cast(str, payload["calendar"]["id"])


def list_calendar(calendar_id: str, scope: str = "profile") -> dict[str, Any]:
    return call(
        tools.calendar_list_events,
        {
            "scope": scope,
            "calendar_id": calendar_id,
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",
        },
    )


def export(calendar_id: str, scope: str = "profile") -> dict[str, Any]:
    return call(
        tools.calendar_export_ics,
        {
            "scope": scope,
            "calendar_id": calendar_id,
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",
        },
    )


def test_import_export_round_trip_is_idempotent_by_uid(hermes_home: Path) -> None:
    del hermes_home
    source = create_calendar("Source")

    first = call(
        tools.calendar_import_ics,
        {"calendar_id": source, "text": SERIES_ICS},
    )
    assert first["ok"] is True
    assert first["imported"] == 3
    assert first["updated"] == 0
    assert first["warnings"] == []

    listed = list_calendar(source)
    assert listed["count"] == 4
    by_summary = {item["summary"]: item for item in listed["events"]}
    assert by_summary["Weekly"]["master_id"] == by_summary["Weekly moved"]["master_id"]
    assert by_summary["Weekly moved"]["is_override"] is True
    assert by_summary["Weekly moved"]["occurrence_start"]["utc"] == "2026-03-09T14:00:00Z"
    assert by_summary["One off"]["occurrence_start"]["utc"] == "2026-03-05T12:00:00Z"

    second = call(
        tools.calendar_import_ics,
        {"calendar_id": source, "text": SERIES_ICS},
    )
    assert second["imported"] == 0
    assert second["updated"] == 3
    assert second["skipped"] == 0
    assert list_calendar(source)["count"] == 4

    exported = export(source)
    assert exported["ok"] is True
    assert exported["count"] == 3
    assert "RRULE:FREQ=WEEKLY;COUNT=4" in exported["ics"]
    assert "RECURRENCE-ID" in exported["ics"]
    assert "EXDATE" in exported["ics"]
    assert exported["ics"].endswith("END:VCALENDAR\r\n")

    target = create_calendar("Target")
    imported_copy = call(
        tools.calendar_import_ics,
        {"calendar_id": target, "text": exported["ics"]},
    )
    assert imported_copy["imported"] == 3
    copied = list_calendar(target)
    assert copied["count"] == 4
    assert {item["summary"] for item in copied["events"]} == {
        "Weekly",
        "Weekly moved",
        "One off",
    }
    reimport = call(
        tools.calendar_import_ics,
        {"calendar_id": target, "text": exported["ics"]},
    )
    assert reimport["imported"] == 0
    assert reimport["updated"] == 3
    assert list_calendar(target)["count"] == 4


def test_import_and_export_work_in_the_global_scope(hermes_home: Path) -> None:
    del hermes_home
    imported = call(
        tools.calendar_import_ics,
        {"scope": "global", "calendar_id": "cal_global", "text": SERIES_ICS},
    )
    assert imported["imported"] == 3

    listed = list_calendar("cal_global", scope="global")
    assert listed["count"] == 4
    exported = export("cal_global", scope="global")
    assert exported["count"] == 3


def test_import_reports_warnings_and_never_raises(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()

    malformed = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": "this is not iCalendar"},
    )
    assert malformed["ok"] is True
    assert malformed["imported"] == 0
    assert malformed["warnings"]

    orphan = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:orphan@example\r\n"
        "RECURRENCE-ID:20260302T090000Z\r\n"
        "SUMMARY:Orphan\r\n"
        "DTSTART:20260302T090000Z\r\n"
        "DTEND:20260302T100000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    skipped = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": orphan},
    )
    assert skipped["imported"] == 0
    assert skipped["skipped"] == 1
    assert any("override without a master" in item for item in skipped["warnings"])


def test_import_skips_duplicate_master_uids(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    duplicate = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:dup@example\r\n"
        "SUMMARY:First\r\n"
        "DTSTART:20260302T090000Z\r\n"
        "DTEND:20260302T100000Z\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:dup@example\r\n"
        "SUMMARY:Second\r\n"
        "DTSTART:20260303T090000Z\r\n"
        "DTEND:20260303T100000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    payload = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": duplicate},
    )

    assert payload["imported"] == 1
    assert payload["skipped"] == 1
    assert any("duplicate master UID" in item for item in payload["warnings"])
    assert list_calendar(calendar_id)["count"] == 1


def test_import_preserves_unsupported_rules_without_expanding_them(
    hermes_home: Path,
) -> None:
    del hermes_home
    calendar_id = create_calendar()
    unsupported = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:unsupported@example\r\n"
        "SUMMARY:Unsupported\r\n"
        "DTSTART:20260302T090000Z\r\n"
        "DTEND:20260302T100000Z\r\n"
        "RRULE:FREQ=WEEKLY;BYHOUR=9\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    payload = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": unsupported},
    )
    assert payload["imported"] == 1
    assert any("unsupported RRULE" in item for item in payload["warnings"])

    listed = list_calendar(calendar_id)
    assert listed["count"] == 1
    item = listed["events"][0]
    assert item["rrule_unsupported"] == ["BYHOUR=9"]
    assert item["occurrence_start"]["utc"] == "2026-03-02T09:00:00Z"
    assert listed["recurrence_warnings"]
    exported = export(calendar_id)
    assert "RRULE:FREQ=WEEKLY;BYHOUR=9" in exported["ics"]


def test_import_size_limit_becomes_a_warning(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del hermes_home
    calendar_id = create_calendar()
    monkeypatch.setattr(service, "MAX_ICS_BYTES", 10)

    payload = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": SERIES_ICS},
    )

    assert payload["ok"] is True
    assert payload["imported"] == 0
    assert payload["updated"] == 0
    assert any("exceeds" in item for item in payload["warnings"])


def test_import_and_export_argument_failures(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()

    assert (
        call(
            tools.calendar_import_ics,
            {"calendar_id": "cal_missing", "text": SERIES_ICS},
        )["code"]
        == "not_found"
    )
    assert (
        call(
            tools.calendar_import_ics,
            {"calendar_id": calendar_id, "text": 5},
        )["code"]
        == "invalid_argument"
    )
    assert (
        call(
            tools.calendar_export_ics,
            {
                "calendar_id": "cal_missing",
                "start": "2026-03-01T00:00:00Z",
                "end": "2026-04-01T00:00:00Z",
            },
        )["code"]
        == "not_found"
    )
    assert (
        call(
            tools.calendar_export_ics,
            {
                "calendar_id": calendar_id,
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-03-01T00:00:00Z",
            },
        )["code"]
        == "invalid_argument"
    )


def test_export_of_empty_calendar_is_a_valid_empty_document(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()

    payload = export(calendar_id)

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["ics"].startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" not in payload["ics"]


def test_all_day_series_imports_expands_and_exports(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    all_day = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:allday@example\r\n"
        "SUMMARY:Holiday\r\n"
        "DTSTART;VALUE=DATE:20260302\r\n"
        "DTEND;VALUE=DATE:20260303\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    imported = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": all_day},
    )
    assert imported["imported"] == 1

    listed = list_calendar(calendar_id)
    assert listed["count"] == 3
    assert [item["occurrence_start"]["date"] for item in listed["events"]] == [
        "2026-03-02",
        "2026-03-09",
        "2026-03-16",
    ]
    assert all(item["all_day"] is True for item in listed["events"])

    master = listed["events"][0]
    rejected = call(
        tools.calendar_update_event,
        {
            "event_id": master["master_id"],
            "expected_rev": master["rev"],
            "occurrence_start": "2026-03-09",
            "start": "2026-03-09T10:00:00Z",
        },
    )
    assert rejected["code"] == "invalid_argument"

    exported = export(calendar_id)
    assert "DTSTART;VALUE=DATE:20260302" in exported["ics"]
    assert "RRULE:FREQ=WEEKLY;COUNT=3" in exported["ics"]


def test_import_override_only_document_uses_an_existing_master(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    assert (
        call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": SERIES_ICS})[
            "imported"
        ]
        == 3
    )
    override_only = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:series-1@example\r\n"
        "RECURRENCE-ID;TZID=America/New_York:20260323T090000\r\n"
        "SUMMARY:Replacement\r\n"
        "DTSTART;TZID=America/New_York:20260323T110000\r\n"
        "DTEND;TZID=America/New_York:20260323T113000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    payload = call(
        tools.calendar_import_ics,
        {"calendar_id": calendar_id, "text": override_only},
    )

    assert payload["imported"] == 1
    assert payload["skipped"] == 0
    listed = list_calendar(calendar_id)
    replacements = [item for item in listed["events"] if item["summary"] == "Replacement"]
    assert len(replacements) == 1
    assert replacements[0]["is_override"] is True


def test_import_flags_unsupported_frequencies_and_malformed_parts(
    hermes_home: Path,
) -> None:
    del hermes_home
    calendar_id = create_calendar()
    hourly = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:hourly@example\r\n"
        "SUMMARY:Hourly\r\n"
        "DTSTART:20260302T090000Z\r\n"
        "DTEND:20260302T100000Z\r\n"
        "RRULE:FREQ=HOURLY\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    payload = call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": hourly})
    assert payload["imported"] == 1
    assert any("FREQ=HOURLY" in item for item in payload["warnings"])
    listed = list_calendar(calendar_id)
    assert listed["events"][0]["rrule_unsupported"] == ["FREQ=HOURLY"]

    malformed = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:malformed@example\r\n"
        "SUMMARY:Malformed\r\n"
        "DTSTART:20260302T110000Z\r\n"
        "DTEND:20260302T120000Z\r\n"
        "RRULE:FREQ=WEEKLY;garbage\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    assert (
        call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": malformed})["imported"]
        == 1
    )
    listed = list_calendar(calendar_id)
    malformed_item = next(item for item in listed["events"] if item["summary"] == "Malformed")
    assert malformed_item["occurrence_start"]["utc"] == "2026-03-02T11:00:00Z"
    assert listed["recurrence_warnings"]

    empty_part = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:empty-part@example\r\n"
        "SUMMARY:Empty part\r\n"
        "DTSTART:20260302T130000Z\r\n"
        "DTEND:20260302T140000Z\r\n"
        "RRULE:FREQ=WEEKLY;;\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    assert (
        call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": empty_part})[
            "imported"
        ]
        == 1
    )
    listed = list_calendar(calendar_id)
    empty_item = next(item for item in listed["events"] if item["summary"] == "Empty part")
    assert empty_item["occurrence_start"]["utc"] == "2026-03-02T13:00:00Z"
    assert listed["recurrence_warnings"]


def test_export_skips_series_and_occurrences_outside_the_window(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    assert (
        call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": SERIES_ICS})[
            "imported"
        ]
        == 3
    )

    empty_window = call(
        tools.calendar_export_ics,
        {
            "calendar_id": calendar_id,
            "start": "2030-03-01T00:00:00Z",
            "end": "2030-04-01T00:00:00Z",
        },
    )
    assert empty_window["ok"] is True
    assert empty_window["count"] == 0


def test_export_omits_tombstoned_overrides(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    event = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "Series",
            "start": "2026-03-02T09:00:00Z",
            "end": "2026-03-02T10:00:00Z",
            "rrule": "FREQ=WEEKLY;COUNT=4",
        },
    )
    override = call(
        tools.calendar_update_event,
        {
            "event_id": event["event"]["id"],
            "expected_rev": event["event"]["rev"],
            "occurrence_start": "2026-03-09T09:00:00Z",
            "summary": "Moved",
        },
    )
    assert override["ok"] is True
    from hermes_calendar.db import Repository, open_profile_store

    connection = open_profile_store()
    try:
        Repository(connection).delete_event(cast(str, override["event"]["id"]))
    finally:
        connection.close()

    exported = export(calendar_id)
    assert exported["ok"] is True
    assert exported["count"] == 1
    assert "Moved" not in exported["ics"]

    deleted = call(
        tools.calendar_delete_event,
        {
            "event_id": event["event"]["id"],
            "expected_rev": event["event"]["rev"],
            "series_scope": "all",
        },
    )
    assert deleted["ok"] is True
    assert "deleted_overrides" not in deleted


def test_floating_series_expands_and_edits_in_utc(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    floating = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:floating@example\r\n"
        "SUMMARY:Floating\r\n"
        "DTSTART:20260302T090000\r\n"
        "DTEND:20260302T093000\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=2\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    assert (
        call(tools.calendar_import_ics, {"calendar_id": calendar_id, "text": floating})["imported"]
        == 1
    )
    listed = list_calendar(calendar_id)
    assert listed["count"] == 2
    assert listed["events"][0]["floating"] is True
    assert listed["events"][0]["occurrence_start"]["tz"] is None
    assert listed["events"][0]["occurrence_start"]["utc"] == "2026-03-02T09:00:00Z"

    updated = call(
        tools.calendar_update_event,
        {
            "event_id": listed["events"][0]["master_id"],
            "expected_rev": listed["events"][0]["rev"],
            "occurrence_start": "2026-03-09T09:00:00",
            "start": "2026-03-09T10:00:00",
            "end": "2026-03-09T10:30:00",
        },
    )
    assert updated["ok"] is True
    assert updated["event"]["floating"] is True
    assert updated["event"]["start"]["utc"] == "2026-03-09T10:00:00Z"

    anchored = call(
        tools.calendar_update_event,
        {
            "event_id": listed["events"][0]["master_id"],
            "expected_rev": listed["events"][0]["rev"],
            "occurrence_start": "2026-03-02T09:00:00",
            "timezone": "America/New_York",
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T09:30:00",
        },
    )
    assert anchored["ok"] is True
    assert anchored["event"]["floating"] is False
    assert anchored["event"]["start"]["tz"] == "America/New_York"


def test_non_recurring_event_round_trips_with_metadata(hermes_home: Path) -> None:
    del hermes_home
    calendar_id = create_calendar()
    created = call(
        tools.calendar_create_event,
        {
            "calendar_id": calendar_id,
            "summary": "Review",
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T10:00:00",
            "timezone": "Europe/Paris",
            "description": "Quarterly",
            "location": "Room 4B",
        },
    )
    assert created["ok"] is True

    exported = export(calendar_id)
    target = create_calendar("Target")
    imported = call(
        tools.calendar_import_ics,
        {"calendar_id": target, "text": exported["ics"]},
    )
    assert imported["imported"] == 1

    copied = list_calendar(target)
    assert copied["count"] == 1
    item = copied["events"][0]
    assert item["summary"] == "Review"
    assert item["description"] == "Quarterly"
    assert item["location"] == "Room 4B"
    assert item["start"]["tz"] == "Europe/Paris"
    assert datetime.fromisoformat(item["start"]["utc"].replace("Z", "+00:00")).year == 2026
