"""Serializer and round-trip tests for :mod:`hermes_calendar.ics`."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from hermes_calendar.ics import parse_ics, serialize_events
from hermes_calendar.models import Attendee, EventDraft, Reminder


def make_event(**overrides: Any) -> EventDraft:
    """Build a valid timed draft with sensible defaults."""
    values: dict[str, Any] = {
        "calendar_id": "cal_test",
        "start_utc": 1772442000,
        "end_utc": 1772445600,
        "uid": "uid@example.com",
        "summary": "Meeting",
        "start_local": "2026-03-02T09:00:00",
        "start_tz": "UTC",
        "end_local": "2026-03-02T10:00:00",
        "end_tz": "UTC",
        "duration_seconds": 3600,
    }
    values.update(overrides)
    return EventDraft(**values)


def round_trip(text: str) -> str:
    """Parse, serialize, and return text."""
    return serialize_events(parse_ics(text).events)


def test_serialize_header_crlf_and_minimal_event() -> None:
    output = serialize_events([make_event()])

    assert output.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:")
    assert "CALSCALE:GREGORIAN\r\n" in output
    assert output.endswith("END:VCALENDAR\r\n")
    assert "\n" not in output.replace("\r\n", "")
    assert "DTSTART:20260302T090000Z\r\n" in output
    assert "DTEND:20260302T100000Z\r\n" in output


def test_serialize_all_day_from_dates() -> None:
    draft = make_event(
        is_all_day=True,
        start_date="2026-03-02",
        end_date="2026-03-04",
        start_utc=1772409600,
        end_utc=1772582400,
        start_local=None,
        start_tz=None,
        end_local=None,
        end_tz=None,
        duration_seconds=None,
    )

    output = serialize_events([draft])

    assert "DTSTART;VALUE=DATE:20260302\r\n" in output
    assert "DTEND;VALUE=DATE:20260304\r\n" in output


def test_serialize_all_day_derives_missing_dates() -> None:
    draft = make_event(
        is_all_day=True,
        start_date=None,
        end_date=None,
        start_utc=1772409600,
        end_utc=1772409600,
        start_local=None,
        start_tz=None,
        end_local=None,
        end_tz=None,
        duration_seconds=None,
    )

    output = serialize_events([draft])

    assert "DTSTART;VALUE=DATE:20260302\r\n" in output
    assert "DTEND;VALUE=DATE:20260303\r\n" in output


def test_serialize_floating_event_has_no_tzid() -> None:
    draft = make_event(
        start_tz=None,
        end_tz=None,
        start_local="2026-03-02T09:00:00",
        end_local="2026-03-02T10:00:00",
        is_floating=True,
    )

    output = serialize_events([draft])

    assert "DTSTART:20260302T090000\r\n" in output
    assert "DTEND:20260302T100000\r\n" in output


def test_serialize_zoned_event_derives_missing_end_wall_clock() -> None:
    draft = make_event(
        start_utc=1772460000,
        end_utc=1772463600,
        start_tz="America/New_York",
        end_tz="America/New_York",
        start_local="2026-03-02T09:00:00",
        end_local=None,
    )

    output = serialize_events([draft])

    assert "DTSTART;TZID=America/New_York:20260302T090000\r\n" in output
    assert "DTEND;TZID=America/New_York:20260302T100000\r\n" in output


def test_serialize_invalid_tzid_still_produces_wall_clock() -> None:
    draft = make_event(start_tz="Mars/Olympus", end_tz="Mars/Olympus")

    output = serialize_events([draft])

    assert "DTSTART;TZID=Mars/Olympus:20260302T090000\r\n" in output


def test_serialize_folds_long_utf8_lines_at_75_octets() -> None:
    summary = "é" * 120
    draft = make_event(summary=summary)

    output = serialize_events([draft])

    for line in output.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert parse_ics(output).events[0].summary == summary


def test_serialize_escapes_text() -> None:
    draft = make_event(summary="a,b;c\\d\ne", description="line1\r\nline2")

    output = serialize_events([draft])

    assert "SUMMARY:a\\,b\\;c\\\\d\\ne\r\n" in output
    parsed = parse_ics(output).events[0]
    assert parsed.summary == "a,b;c\\d\ne"
    assert parsed.description == "line1\nline2"


def test_serialize_recurrence_id_and_range() -> None:
    draft = make_event(
        start_tz="America/New_York",
        end_tz="America/New_York",
        recurrence_id_raw="2026-03-16T09:00:00",
        recurrence_key="T:1773666000",
        recurrence_range="THISANDFUTURE",
    )

    output = serialize_events([draft])

    assert "RECURRENCE-ID;TZID=America/New_York;RANGE=THISANDFUTURE:20260316T090000\r\n" in output


def test_serialize_recurrence_values_group_by_form() -> None:
    draft = make_event(
        start_tz="America/New_York",
        end_tz="America/New_York",
        exdate=("2026-03-16T09:00:00", "2026-03-23T09:00:00"),
        rdate=("2026-03-30T13:00:00Z",),
    )

    output = serialize_events([draft])

    assert "EXDATE;TZID=America/New_York:20260316T090000,20260323T090000\r\n" in output
    assert "RDATE:20260330T130000Z\r\n" in output


def test_serialize_recurrence_values_for_utc_and_all_day() -> None:
    utc_draft = make_event(
        exdate=("2026-03-16T09:00:00", "2026-03-23T09:00:00Z"),
    )
    all_day = make_event(
        is_all_day=True,
        start_date="2026-03-02",
        end_date="2026-03-03",
        start_utc=1772409600,
        end_utc=1772496000,
        start_local=None,
        start_tz=None,
        end_local=None,
        end_tz=None,
        duration_seconds=None,
        exdate=("2026-03-16",),
    )

    output = serialize_events([utc_draft, all_day])

    assert "EXDATE:20260316T090000Z,20260323T090000Z\r\n" in output
    assert "EXDATE;VALUE=DATE:20260316\r\n" in output


def test_serialize_floating_recurrence_values_have_no_zone() -> None:
    draft = make_event(start_tz=None, end_tz=None, is_floating=True)

    output = serialize_events([replace(draft, exdate=("2026-03-16T09:00:00",))])

    assert "EXDATE:20260316T090000\r\n" in output


def test_serialize_status_transparency_class_sequence_and_url() -> None:
    draft = make_event(
        status="CANCELLED",
        transparency="TRANSPARENT",
        classification="CONFIDENTIAL",
        sequence=3,
        url="https://example.com/e",
        categories=("work", "design"),
        created_utc=1769904000,
        last_modified_utc=1769990400,
        dtstamp_utc=1772366400,
    )

    output = serialize_events([draft])

    assert "STATUS:CANCELLED\r\n" in output
    assert "TRANSP:TRANSPARENT\r\n" in output
    assert "CLASS:CONFIDENTIAL\r\n" in output
    assert "SEQUENCE:3\r\n" in output
    assert "URL:https://example.com/e\r\n" in output
    assert "CATEGORIES:work,design\r\n" in output
    assert "DTSTAMP:20260301T120000Z\r\n" in output
    assert "CREATED:20260201T000000Z\r\n" in output
    assert "LAST-MODIFIED:20260202T000000Z\r\n" in output


def test_serialize_organizer_and_attendees() -> None:
    draft = make_event(
        organizer_email="dana@example.com",
        organizer_name="Dana, Sr.",
        attendees=(
            Attendee(
                address="mailto:alex@example.com",
                name="Alex",
                role="CHAIR",
                partstat="ACCEPTED",
                rsvp=True,
                cutype="INDIVIDUAL",
                delegated_to="mailto:sam@example.com",
                delegated_from="mailto:kim@example.com",
                sent_by="mailto:pa@example.com",
            ),
            Attendee(address="mailto:sam@example.com"),
        ),
    )

    output = serialize_events([draft]).replace("\r\n ", "")

    assert 'ORGANIZER;CN="Dana, Sr.":mailto:dana@example.com\r\n' in output
    assert (
        "ATTENDEE;CN=Alex;ROLE=CHAIR;PARTSTAT=ACCEPTED;RSVP=TRUE;"
        'CUTYPE=INDIVIDUAL;DELEGATED-TO="mailto:sam@example.com";'
        'DELEGATED-FROM="mailto:kim@example.com";SENT-BY="mailto:pa@example.com":'
        "mailto:alex@example.com\r\n"
    ) in output
    assert "ATTENDEE:mailto:sam@example.com\r\n" in output


def test_serialize_organizer_name_only() -> None:
    output = serialize_events([make_event(organizer_name="Dana")])

    assert "ORGANIZER;CN=Dana:\r\n" in output


def test_serialize_xprops_and_alarms() -> None:
    draft = make_event(
        xprops={"X-CUSTOM": ["X-CUSTOM:one", "X-CUSTOM:two"], "PRIORITY": "PRIORITY:5"},
        reminders=(
            Reminder(
                action="DISPLAY",
                related="END",
                offset_seconds=-600,
                repeat_count=2,
                repeat_interval_seconds=300,
                description="Soon",
            ),
            Reminder(
                action="AUDIO",
                trigger_kind="ABSOLUTE",
                absolute_utc=1772440200,
            ),
        ),
    )

    output = serialize_events([draft])

    assert "X-CUSTOM:one\r\nX-CUSTOM:two\r\n" in output
    assert "PRIORITY:5\r\n" in output
    assert "TRIGGER;RELATED=END:-PT10M\r\n" in output
    assert "REPEAT:2\r\n" in output
    assert "DURATION:PT5M\r\n" in output
    assert "TRIGGER;VALUE=DATE-TIME:20260302T083000Z\r\n" in output


def test_serialize_absolute_alarm_without_instant_falls_back_relative() -> None:
    output = serialize_events([make_event(reminders=(Reminder(trigger_kind="ABSOLUTE"),))])

    assert "TRIGGER:PT0S\r\n" in output


def test_serialize_synthetic_uid_is_deterministic() -> None:
    first = serialize_events([make_event(uid=None)])
    second = serialize_events([make_event(uid=None)])

    assert first == second
    assert "@hermes-calendar.invalid" in first


def test_serialize_round_trips_parsed_event() -> None:
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:round@example.com\r\n"
        "DTSTAMP:20260301T120000Z\r\n"
        "SUMMARY:Weekly review\\, team\r\n"
        "DESCRIPTION:Line one\\nLine two\r\n"
        "LOCATION:Room 4B\r\n"
        "DTSTART;TZID=America/New_York:20260302T090000\r\n"
        "DTEND;TZID=America/New_York:20260302T100000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
        "EXDATE;TZID=America/New_York:20260316T090000\r\n"
        "STATUS:TENTATIVE\r\n"
        "X-CUSTOM;X-PARAM=1:value\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\n"
        "DESCRIPTION:Soon\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    first = parse_ics(text)
    output = serialize_events(first.events)
    second = parse_ics(output)

    assert second.warnings == []
    assert second.events == first.events


def test_serialize_round_trips_all_day_event() -> None:
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:day@example.com\r\n"
        "DTSTART;VALUE=DATE:20260302\r\n"
        "DTEND;VALUE=DATE:20260304\r\n"
        "SUMMARY:Holiday\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    first = parse_ics(text)
    second = parse_ics(serialize_events(first.events))

    assert second.events == first.events


def test_serialize_round_trips_floating_and_utc() -> None:
    for dtstart, dtend in (
        ("DTSTART:20260302T090000", "DTEND:20260302T100000"),
        ("DTSTART:20260302T090000Z", "DTEND:20260302T100000Z"),
    ):
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            f"UID:kind@example.com\r\n{dtstart}\r\n{dtend}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        first = parse_ics(text)
        second = parse_ics(serialize_events(first.events))
        assert second.events == first.events


def test_serialize_dst_end_uses_absolute_end_for_utc() -> None:
    start = datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York"))
    end = start + timedelta(hours=1)

    draft = make_event(
        start_utc=int(start.timestamp()),
        end_utc=int(end.timestamp()),
        start_tz="UTC",
        end_tz="UTC",
        start_local=None,
        end_local=None,
        duration_seconds=3600,
    )
    output = serialize_events([draft])

    assert "DTSTART:20260308T063000Z\r\n" in output
    assert "DTEND:20260308T073000Z\r\n" in output


def test_serialize_skips_empty_xprops_and_cycle_free() -> None:
    output = serialize_events([make_event(xprops={"X-EMPTY": ""})])

    assert "X-EMPTY" not in output


def test_serialize_utc_floor_of_recurrence_value() -> None:
    draft = make_event(exdate=("20260316T090000",))

    output = serialize_events([draft])

    assert "EXDATE:20260316T090000Z\r\n" in output


def test_serialize_recurrence_id_utc() -> None:
    draft = make_event(recurrence_id_raw="2026-03-16T09:00:00Z")

    output = serialize_events([draft])

    assert "RECURRENCE-ID:20260316T090000Z\r\n" in output


def test_serialize_invalid_tzid_without_stored_wall_clock_falls_back_to_utc() -> None:
    draft = make_event(
        start_tz="Mars/Olympus",
        end_tz="Mars/Olympus",
        start_local=None,
        end_local=None,
    )

    output = serialize_events([draft])

    assert "DTSTART;TZID=Mars/Olympus:20260302T090000\r\n" in output
    assert "DTEND;TZID=Mars/Olympus:20260302T100000\r\n" in output


def test_serialize_organizer_email_only() -> None:
    output = serialize_events([make_event(organizer_email="dana@example.com")])

    assert "ORGANIZER:mailto:dana@example.com\r\n" in output


def test_serialize_organizer_email_with_scheme_is_not_doubled() -> None:
    output = serialize_events([make_event(organizer_email="mailto:dana@example.com")])

    assert "ORGANIZER:mailto:dana@example.com\r\n" in output


def test_serialize_alarm_duration_days_and_hours() -> None:
    draft = make_event(
        reminders=(
            Reminder(offset_seconds=86_400),
            Reminder(offset_seconds=90_061),
        )
    )

    output = serialize_events([draft])

    assert "TRIGGER:P1D\r\n" in output
    assert "TRIGGER:P1DT1H1M1S\r\n" in output
