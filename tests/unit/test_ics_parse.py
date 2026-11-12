"""Parser tests for :mod:`hermes_calendar.ics`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_calendar.ics import MAX_EVENTS, MAX_ICS_BYTES, IcsLimitError, parse_ics


def wrap(*body: str) -> str:
    """Build a CRLF VCALENDAR around ``body`` lines."""
    inner = "\r\n".join(body)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{inner}\r\nEND:VCALENDAR\r\n"


def event(*body: str) -> str:
    """Wrap ``body`` lines in VEVENT, then VCALENDAR."""
    return wrap("BEGIN:VEVENT", *body, "END:VEVENT")


def utc_text(epoch: int) -> str:
    """Render an epoch as an ISO UTC timestamp for assertions."""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_parse_timed_tzid_and_round_trip() -> None:
    text = event(
        "UID:abc@example.com",
        "DTSTAMP:20260301T120000Z",
        "SUMMARY:Weekly review\\, team",
        "DESCRIPTION:Line one\\nLine two",
        "LOCATION:Room 4B\\; second floor",
        "DTSTART;TZID=America/New_York:20260302T090000",
        "DTEND;TZID=America/New_York:20260302T100000",
        "STATUS:TENTATIVE",
        "TRANSP:TRANSPARENT",
        "CLASS:PRIVATE",
        "SEQUENCE:2",
        "CATEGORIES:work,design",
        "URL:https://example.com/meeting",
        "CREATED:20260201T000000Z",
        "LAST-MODIFIED:20260202T000000Z",
    )

    result = parse_ics(text)

    assert result.warnings == []
    draft = result.events[0]
    assert draft.summary == "Weekly review, team"
    assert draft.description == "Line one\nLine two"
    assert draft.location == "Room 4B; second floor"
    assert draft.start_local == "2026-03-02T09:00:00"
    assert draft.start_tz == "America/New_York"
    assert draft.end_local == "2026-03-02T10:00:00"
    assert draft.duration_seconds == 3600
    assert utc_text(draft.start_utc) == "2026-03-02T14:00:00+00:00"
    assert draft.status == "TENTATIVE"
    assert draft.transparency == "TRANSPARENT"
    assert draft.classification == "PRIVATE"
    assert draft.sequence == 2
    assert draft.categories == ("work", "design")
    assert draft.url == "https://example.com/meeting"
    assert draft.created_utc == 1769904000
    assert draft.last_modified_utc == 1769990400
    assert draft.dtstamp_utc == 1772366400


def test_parse_dst_spring_forward_uses_pre_gap_offset() -> None:
    text = event(
        "UID:spring",
        "DTSTART;TZID=America/New_York:20260308T023000",
        "DTEND;TZID=America/New_York:20260308T043000",
    )

    draft = parse_ics(text).events[0]

    assert draft.start_utc == 1772955000
    assert utc_text(draft.start_utc) == "2026-03-08T07:30:00+00:00"
    assert draft.start_local == "2026-03-08T02:30:00"
    assert draft.duration_seconds == 3600


def test_parse_dst_fall_back_uses_first_occurrence() -> None:
    text = event(
        "UID:fall",
        "DTSTART;TZID=America/New_York:20261101T013000",
        "DTEND;TZID=America/New_York:20261101T023000",
    )

    draft = parse_ics(text).events[0]

    assert draft.start_utc == 1793511000
    assert utc_text(draft.start_utc) == "2026-11-01T05:30:00+00:00"


def test_parse_all_day_is_date_based_with_exclusive_end() -> None:
    text = event(
        "UID:all-day",
        "SUMMARY:Conference",
        "DTSTART;VALUE=DATE:20260302",
        "DTEND;VALUE=DATE:20260304",
    )

    draft = parse_ics(text).events[0]

    assert draft.is_all_day
    assert not draft.is_floating
    assert draft.start_date == "2026-03-02"
    assert draft.end_date == "2026-03-04"
    assert draft.start_local is None
    assert draft.start_tz is None
    assert draft.duration_seconds is None
    assert draft.start_utc == 1772409600
    assert draft.end_utc == 1772582400


def test_parse_all_day_without_end_uses_one_day() -> None:
    draft = parse_ics(event("UID:one-day", "DTSTART;VALUE=DATE:20260302")).events[0]

    assert draft.end_date == "2026-03-03"


def test_parse_all_day_duration_rounds_up() -> None:
    result = parse_ics(
        event(
            "UID:days",
            "DTSTART;VALUE=DATE:20260302",
            "DURATION:P2DT1H",
        )
    )

    draft = result.events[0]
    assert draft.end_date == "2026-03-05"
    assert any("rounded up" in warning for warning in result.warnings)


def test_parse_timed_duration_becomes_end_and_duration() -> None:
    draft = parse_ics(
        event(
            "UID:duration",
            "DTSTART;TZID=America/New_York:20260302T090000",
            "DURATION:PT90M",
        )
    ).events[0]

    assert draft.start_local == "2026-03-02T09:00:00"
    assert draft.end_local == "2026-03-02T10:30:00"
    assert draft.duration_seconds == 5400
    assert draft.end_utc - draft.start_utc == 5400


def test_parse_folded_lines_and_tab_continuation() -> None:
    text = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:folded\r\n"
        "DTSTART:20260302T090000Z\r\n"
        "SUMMARY:A very long summary that is folded acro\r\n"
        " ss two physical lines\r\n"
        "DESCRIPTION:first\r\n"
        "\tsecond\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    result = parse_ics(text)

    assert result.warnings == []
    assert (
        result.events[0].summary == "A very long summary that is folded across two physical lines"
    )
    assert result.events[0].description == "firstsecond"


def test_parse_text_escapes() -> None:
    draft = parse_ics(
        event(
            "UID:escapes",
            "DTSTART:20260302T090000Z",
            "SUMMARY:comma\\, semicolon\\; backslash\\\\ newline\\nend",
        )
    ).events[0]

    assert draft.summary == "comma, semicolon; backslash\\ newline\nend"


def test_parse_floating_event() -> None:
    draft = parse_ics(
        event("UID:floating", "DTSTART:20260302T090000", "DTEND:20260302T100000")
    ).events[0]

    assert draft.is_floating
    assert draft.start_tz is None
    assert draft.start_local == "2026-03-02T09:00:00"
    assert draft.duration_seconds == 3600


def test_parse_utc_event() -> None:
    draft = parse_ics(
        event("UID:utc", "DTSTART:20260302T090000Z", "DTEND:20260302T100000Z")
    ).events[0]

    assert not draft.is_floating
    assert draft.start_tz == "UTC"
    assert draft.start_local == "2026-03-02T09:00:00"
    assert draft.start_utc == 1772442000


def test_parse_recurrence_override() -> None:
    text = event(
        "UID:series",
        "DTSTART;TZID=America/New_York:20260302T090000",
        "DTEND;TZID=America/New_York:20260302T100000",
        "RECURRENCE-ID;TZID=America/New_York;RANGE=THISANDFUTURE:20260316T090000",
        "SUMMARY:Moved",
    )

    result = parse_ics(text)

    draft = result.events[0]
    assert draft.recurrence_id_raw == "2026-03-16T09:00:00"
    assert draft.recurrence_key == "T:1773666000"
    assert draft.recurrence_range == "THISANDFUTURE"


def test_parse_all_day_override_key() -> None:
    draft = parse_ics(
        event(
            "UID:series",
            "DTSTART;VALUE=DATE:20260302",
            "RECURRENCE-ID;VALUE=DATE:20260316",
        )
    ).events[0]

    assert draft.recurrence_key == "D:2026-03-16"


def test_parse_override_rrule_is_dropped_with_warning() -> None:
    result = parse_ics(
        event(
            "UID:series",
            "DTSTART:20260316T090000Z",
            "RECURRENCE-ID:20260316T090000Z",
            "RRULE:FREQ=WEEKLY",
        )
    )

    assert result.events[0].rrule is None
    assert any("override carries RRULE" in warning for warning in result.warnings)


def test_parse_rrule_unsupported_parts_are_flagged() -> None:
    draft = parse_ics(
        event(
            "UID:rrule",
            "DTSTART:20260302T090000Z",
            "RRULE:FREQ=WEEKLY;BYDAY=MO;BYSECOND=30;RSCALE=GREGORIAN",
        )
    ).events[0]

    assert draft.rrule == "FREQ=WEEKLY;BYDAY=MO;BYSECOND=30;RSCALE=GREGORIAN"
    assert draft.rrule_unsupported == ("BYSECOND=30", "RSCALE=GREGORIAN")


def test_parse_rrule_supported_only_has_no_unsupported_list() -> None:
    draft = parse_ics(
        event("UID:rrule", "DTSTART:20260302T090000Z", "RRULE:FREQ=DAILY;COUNT=3")
    ).events[0]

    assert draft.rrule_unsupported is None


def test_parse_empty_rrule_warns() -> None:
    result = parse_ics(event("UID:rrule", "DTSTART:20260302T090000Z", "RRULE:"))

    assert result.events[0].rrule is None
    assert any("empty RRULE" in warning for warning in result.warnings)


def test_parse_exdate_rdate_and_period() -> None:
    result = parse_ics(
        event(
            "UID:recurrence",
            "DTSTART;TZID=America/New_York:20260302T090000",
            "EXDATE;TZID=America/New_York:20260316T090000,20260323T090000",
            "RDATE:20260330T130000Z",
            "RDATE;VALUE=PERIOD:20260406T130000Z/20260406T140000Z",
        )
    )

    draft = result.events[0]
    assert draft.exdate == ("2026-03-16T09:00:00", "2026-03-23T09:00:00")
    assert draft.rdate == ("2026-03-30T13:00:00Z", "2026-04-06T13:00:00Z")
    assert any("PERIOD reduced" in warning for warning in result.warnings)


def test_parse_valarm_relative_absolute_and_repeat() -> None:
    text = event(
        "UID:alarms",
        "DTSTART:20260302T090000Z",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "TRIGGER;RELATED=END:-PT15M",
        "DESCRIPTION:Wrap up",
        "REPEAT:2",
        "DURATION:PT5M",
        "END:VALARM",
        "BEGIN:VALARM",
        "ACTION:AUDIO",
        "TRIGGER;VALUE=DATE-TIME:20260302T083000Z",
        "END:VALARM",
    )

    reminders = parse_ics(text).events[0].reminders

    assert reminders[0].action == "DISPLAY"
    assert reminders[0].related == "END"
    assert reminders[0].offset_seconds == -900
    assert reminders[0].repeat_count == 2
    assert reminders[0].repeat_interval_seconds == 300
    assert reminders[0].description == "Wrap up"
    assert reminders[1].action == "AUDIO"
    assert reminders[1].trigger_kind == "ABSOLUTE"
    assert reminders[1].absolute_utc == 1772440200


def test_parse_unknown_properties_are_preserved_raw() -> None:
    result = parse_ics(
        event(
            "UID:xprops",
            "DTSTART:20260302T090000Z",
            "X-CUSTOM;X-PARAM=1:value",
            "PRIORITY:5",
            "X-CUSTOM:second",
        )
    )

    draft = result.events[0]
    assert draft.xprops == {
        "X-CUSTOM": ["X-CUSTOM;X-PARAM=1:value", "X-CUSTOM:second"],
        "PRIORITY": "PRIORITY:5",
    }
    assert result.warnings == []


def test_parse_unknown_calendar_property_warns() -> None:
    result = parse_ics("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Work\r\nEND:VCALENDAR\r\n")

    assert any("X-WR-CALNAME" in warning for warning in result.warnings)


def test_parse_organizer_attendees_and_cutype() -> None:
    text = event(
        "UID:people",
        "DTSTART:20260302T090000Z",
        "ORGANIZER;CN=Dana:mailto:dana@example.com",
        "ATTENDEE;CN=Alex;ROLE=CHAIR;PARTSTAT=ACCEPTED;RSVP=TRUE;"
        "CUTYPE=INDIVIDUAL:mailto:alex@example.com",
        "ATTENDEE:mailto:sam@example.com",
    )

    draft = parse_ics(text).events[0]

    assert draft.organizer_email == "dana@example.com"
    assert draft.organizer_name == "Dana"
    assert draft.attendees[0].email == "alex@example.com"
    assert draft.attendees[0].name == "Alex"
    assert draft.attendees[0].role == "CHAIR"
    assert draft.attendees[0].partstat == "ACCEPTED"
    assert draft.attendees[0].rsvp
    assert draft.attendees[0].cutype == "INDIVIDUAL"
    assert draft.attendees[1].email == "sam@example.com"
    assert draft.attendees[1].role == "REQ-PARTICIPANT"


def test_parse_rejects_oversized_document() -> None:
    with pytest.raises(IcsLimitError, match="exceeds"):
        parse_ics("X" * 11, max_bytes=10)


def test_parse_caps_event_count() -> None:
    text = wrap(
        "BEGIN:VEVENT",
        "UID:first",
        "DTSTART:20260302T090000Z",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:second",
        "DTSTART:20260303T090000Z",
        "END:VEVENT",
    )

    result = parse_ics(text, max_events=1)

    assert [draft.uid for draft in result.events] == ["first"]
    assert any("event count exceeds" in warning for warning in result.warnings)


def test_parse_limits_have_expected_defaults() -> None:
    assert MAX_ICS_BYTES == 5 * 1024 * 1024
    assert MAX_EVENTS == 10_000


def test_parse_non_string_input_raises() -> None:
    with pytest.raises(TypeError, match="str"):
        parse_ics(b"BEGIN:VCALENDAR")  # type: ignore[arg-type]


def test_parse_missing_uid_generates_stable_uid() -> None:
    text = event("DTSTART:20260302T090000Z", "SUMMARY:No identity")

    first = parse_ics(text)
    second = parse_ics(text)

    assert first.events[0].uid == second.events[0].uid
    assert first.events[0].uid is not None
    assert first.events[0].uid.endswith("@hermes-calendar.invalid")
    assert any("missing UID" in warning for warning in first.warnings)


def test_parse_unknown_tzid_warns_and_uses_utc() -> None:
    result = parse_ics(event("UID:tz", "DTSTART;TZID=Mars/Olympus:20260302T090000"))

    draft = result.events[0]
    assert draft.start_tz == "UTC"
    assert draft.start_utc == 1772442000
    assert any("unknown TZID" in warning for warning in result.warnings)


def test_parse_ignores_unsupported_components() -> None:
    result = parse_ics(
        wrap(
            "BEGIN:VTODO",
            "UID:todo",
            "END:VTODO",
            "BEGIN:VJOURNAL",
            "UID:journal",
            "END:VJOURNAL",
            "BEGIN:VOTHER",
            "END:VOTHER",
        )
    )

    assert result.events == []
    assert any("VTODO" in warning for warning in result.warnings)
    assert any("VJOURNAL" in warning for warning in result.warnings)
    assert any("VOTHER" in warning for warning in result.warnings)


def test_parse_events_outside_vcalendar_still_parse() -> None:
    result = parse_ics("BEGIN:VEVENT\r\nUID:loose\r\nDTSTART:20260302T090000Z\r\nEND:VEVENT\r\n")

    assert result.events[0].uid == "loose"
    assert any("no VCALENDAR" in warning for warning in result.warnings)


def test_parse_multiple_calendars() -> None:
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:one\r\nDTSTART:20260302T090000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:two\r\nDTSTART:20260303T090000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    assert [draft.uid for draft in parse_ics(text).events] == ["one", "two"]


@pytest.mark.parametrize(
    ("name", "text", "warning"),
    [
        ("no colon", event("UID:no-colon", "DTSTART:20260302T090000Z", "BROKEN"), "property name"),
        ("bad param", event("UID:p", "DTSTART;X:20260302T090000Z"), "parameter without"),
        (
            "bad date",
            event("UID:d", "DTSTART;VALUE=DATE:not-a-date"),
            "invalid DATE",
        ),
        (
            "bad datetime",
            event("UID:d", "DTSTART:not-a-datetime"),
            "invalid DATE-TIME",
        ),
        ("empty dtstart", event("UID:d", "DTSTART:"), "empty value"),
        ("missing dtstart", event("UID:d", "SUMMARY:none"), "missing DTSTART"),
        (
            "unexpected end",
            "BEGIN:VCALENDAR\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            "unexpected END",
        ),
        (
            "unterminated",
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\n",
            "unterminated",
        ),
    ],
)
def test_parse_malformed_input_warns_without_crashing(name: str, text: str, warning: str) -> None:
    result = parse_ics(text)

    assert result.events == [] or name in {"bad param", "no colon"}
    assert any(warning in item for item in result.warnings), name


def test_parse_handles_garbage_and_empty_input() -> None:
    assert parse_ics("").events == []
    assert parse_ics("\r\n\r\nrandom text\r\n").events == []


def test_parse_both_dtend_and_duration_prefers_dtend() -> None:
    result = parse_ics(
        event(
            "UID:both",
            "DTSTART:20260302T090000Z",
            "DTEND:20260302T100000Z",
            "DURATION:PT5H",
        )
    )

    assert result.events[0].duration_seconds == 3600
    assert any("both DTEND and DURATION" in warning for warning in result.warnings)


def test_parse_dtend_before_dtstart_is_clamped() -> None:
    result = parse_ics(
        event(
            "UID:backwards",
            "DTSTART:20260302T100000Z",
            "DTEND:20260302T090000Z",
        )
    )

    assert result.events[0].duration_seconds == 0
    assert any("clamped" in warning for warning in result.warnings)


def test_parse_all_day_rejects_short_end() -> None:
    result = parse_ics(
        event(
            "UID:short",
            "DTSTART;VALUE=DATE:20260302",
            "DTEND;VALUE=DATE:20260302",
        )
    )

    assert result.events[0].end_date == "2026-03-03"
    assert any("not after DTSTART" in warning for warning in result.warnings)


def test_parse_invalid_values_warn_and_fall_back() -> None:
    result = parse_ics(
        event(
            "UID:invalid",
            "DTSTART:20260302T090000Z",
            "DTEND:not-a-date",
            "DURATION:nope",
            "STATUS:MAYBE",
            "TRANSP:MAYBE",
            "CLASS:MAYBE",
            "SEQUENCE:abc",
            "RECURRENCE-ID:20260302T090000Z;RANGE=NOPE",
        )
    )

    assert result.events[0].status == "CONFIRMED"
    assert result.events[0].transparency == "OPAQUE"
    assert result.events[0].classification == "PUBLIC"
    assert result.events[0].sequence == 0
    assert any("invalid DURATION" in warning for warning in result.warnings)
    assert any("unknown STATUS" in warning for warning in result.warnings)
    assert any("unknown TRANSP" in warning for warning in result.warnings)
    assert any("unknown CLASS" in warning for warning in result.warnings)
    assert any("invalid SEQUENCE" in warning for warning in result.warnings)


def test_parse_invalid_alarm_values_warn() -> None:
    result = parse_ics(
        event(
            "UID:bad-alarms",
            "DTSTART:20260302T090000Z",
            "BEGIN:VALARM",
            "ACTION:NOPE",
            "TRIGGER:garbage",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER;VALUE=DATE-TIME:20260302",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER:PT0S",
            "REPEAT:-1",
            "DURATION:PT0S",
            "RELATED:END",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER:PXYZ",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER:-PT10M",
            "REPEAT:2",
            "END:VALARM",
        )
    )

    assert any("unknown ACTION" in warning for warning in result.warnings)
    assert any("invalid relative TRIGGER" in warning for warning in result.warnings)
    assert any("missing TRIGGER" in warning for warning in result.warnings)
    assert any("unusable absolute TRIGGER" in warning for warning in result.warnings)
    assert any("invalid DURATION" in warning for warning in result.warnings)
    assert any("REPEAT without" in warning for warning in result.warnings)


def test_parse_alarm_unknown_related_warns() -> None:
    result = parse_ics(
        event(
            "UID:related",
            "DTSTART:20260302T090000Z",
            "BEGIN:VALARM",
            "TRIGGER;RELATED=SOMEWHERE:-PT10M",
            "END:VALARM",
        )
    )

    assert result.events[0].reminders[0].related == "START"
    assert any("unknown RELATED" in warning for warning in result.warnings)


def test_parse_alarm_unknown_component_warns() -> None:
    result = parse_ics(
        event(
            "UID:alarm-child",
            "DTSTART:20260302T090000Z",
            "BEGIN:VALARM",
            "TRIGGER:-PT10M",
            "BEGIN:VUNKNOWN",
            "END:VUNKNOWN",
            "END:VALARM",
        )
    )

    assert len(result.events[0].reminders) == 1
    assert any("unsupported component VUNKNOWN" in warning for warning in result.warnings)


def test_parse_attendee_problems_warn() -> None:
    result = parse_ics(
        event(
            "UID:attendees",
            "DTSTART:20260302T090000Z",
            "ORGANIZER;CN=Nobody:",
            "ATTENDEE:",
            "ATTENDEE;ROLE=KING;PARTSTAT=SURE;CUTYPE=ALIEN:mailto:x@example.com",
        )
    )

    draft = result.events[0]
    assert draft.organizer_email is None
    assert draft.organizer_name == "Nobody"
    assert len(draft.attendees) == 1
    assert draft.attendees[0].role == "REQ-PARTICIPANT"
    assert draft.attendees[0].partstat == "NEEDS-ACTION"
    assert draft.attendees[0].cutype is None
    assert any("ORGANIZER has no usable address" in warning for warning in result.warnings)
    assert any("ATTENDEE without an address" in warning for warning in result.warnings)
    assert any("unknown ROLE" in warning for warning in result.warnings)
    assert any("unknown PARTSTAT" in warning for warning in result.warnings)
    assert any("unknown CUTYPE" in warning for warning in result.warnings)


def test_parse_date_valued_instant_properties_warn() -> None:
    result = parse_ics(
        event(
            "UID:dates",
            "DTSTART:20260302T090000Z",
            "DTSTAMP;VALUE=DATE:20260301",
            "CREATED;VALUE=DATE:20260301",
        )
    )

    assert result.events[0].dtstamp_utc is None
    assert any("DTSTAMP must be a DATE-TIME" in warning for warning in result.warnings)


def test_parse_iso_offset_datetime_is_converted() -> None:
    draft = parse_ics(event("UID:offset", "DTSTART:2026-03-02T09:00:00+02:00")).events[0]

    assert draft.start_tz == "UTC"
    assert utc_text(draft.start_utc) == "2026-03-02T07:00:00+00:00"


def test_parse_datetime_without_seconds_is_tolerated() -> None:
    draft = parse_ics(event("UID:short", "DTSTART;TZID=America/New_York:20260302T0900")).events[0]

    assert draft.start_local == "2026-03-02T09:00:00"


def test_parse_timed_event_date_dtend_is_ignored() -> None:
    result = parse_ics(
        event(
            "UID:mismatch",
            "DTSTART:20260302T090000Z",
            "DTEND;VALUE=DATE:20260303",
        )
    )

    assert result.events[0].duration_seconds == 0
    assert any("DTEND must be a DATE-TIME" in warning for warning in result.warnings)


def test_parse_all_day_datetime_dtend_is_ignored() -> None:
    result = parse_ics(
        event(
            "UID:mismatch",
            "DTSTART;VALUE=DATE:20260302",
            "DTEND:20260303T090000Z",
        )
    )

    assert result.events[0].end_date == "2026-03-03"
    assert any("all-day DTEND must be a DATE" in warning for warning in result.warnings)


def test_parse_negative_timed_duration_is_zero() -> None:
    result = parse_ics(
        event(
            "UID:negative",
            "DTSTART:20260302T090000Z",
            "DURATION:-PT1H",
        )
    )

    assert result.events[0].duration_seconds == 0
    assert any("negative DURATION" in warning for warning in result.warnings)


def test_parse_all_day_non_positive_duration_uses_one_day() -> None:
    result = parse_ics(
        event(
            "UID:negative-days",
            "DTSTART;VALUE=DATE:20260302",
            "DURATION:PT0S",
        )
    )

    assert result.events[0].end_date == "2026-03-03"
    assert any("non-positive all-day DURATION" in warning for warning in result.warnings)


def test_parse_quoted_parameters_and_escaped_comma() -> None:
    draft = parse_ics(
        event(
            "UID:quoted",
            "DTSTART:20260302T090000Z",
            'ATTENDEE;CN="Smith; John":mailto:john@example.com',
            "CATEGORIES:a\\,b,c",
        )
    ).events[0]

    assert draft.attendees[0].name == "Smith; John"
    assert draft.categories == ("a,b", "c")


def test_parse_duration_without_components_warns() -> None:
    result = parse_ics(event("UID:empty-duration", "DTSTART:20260302T090000Z", "DURATION:P"))

    assert result.events[0].duration_seconds == 0
    assert any("invalid DURATION" in warning for warning in result.warnings)


def test_parse_loose_event_next_to_a_calendar() -> None:
    text = (
        "BEGIN:VEVENT\r\nUID:loose\r\nDTSTART:20260302T090000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:inside\r\nDTSTART:20260303T090000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    result = parse_ics(text)

    assert [draft.uid for draft in result.events] == ["inside", "loose"]
    assert any("outside VCALENDAR" in warning for warning in result.warnings)


def test_parse_nested_vcalendar() -> None:
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:nested\r\nDTSTART:20260302T090000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\nEND:VCALENDAR\r\n"
    )

    assert parse_ics(text).events[0].uid == "nested"


def test_parse_caps_after_multiple_extra_events() -> None:
    text = wrap(
        "BEGIN:VEVENT",
        "UID:one",
        "DTSTART:20260302T090000Z",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:two",
        "DTSTART:20260303T090000Z",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:three",
        "DTSTART:20260304T090000Z",
        "END:VEVENT",
    )

    result = parse_ics(text, max_events=1)

    assert len(result.events) == 1
    assert sum("event count exceeds" in item for item in result.warnings) == 1


def test_parse_empty_property_name_warns() -> None:
    result = parse_ics("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n;X=1:value\r\nEND:VCALENDAR\r\n")

    assert any("property name" in warning for warning in result.warnings)


def test_parse_floating_event_with_utc_end() -> None:
    draft = parse_ics(
        event(
            "UID:mixed-end",
            "DTSTART:20260302T090000",
            "DTEND:20260302T100000Z",
        )
    ).events[0]

    assert draft.is_floating
    assert draft.end_local == "2026-03-02T10:00:00"


def test_parse_zoned_event_with_floating_end() -> None:
    draft = parse_ics(
        event(
            "UID:mixed-end",
            "DTSTART;TZID=America/New_York:20260302T090000",
            "DTEND:20260302T100000",
        )
    ).events[0]

    assert draft.end_local == "2026-03-02T10:00:00"
    assert draft.end_utc - draft.start_utc == 3600


def test_parse_floating_event_with_duration() -> None:
    draft = parse_ics(
        event(
            "UID:floating-duration",
            "DTSTART:20260302T090000",
            "DURATION:PT1H",
        )
    ).events[0]

    assert draft.end_local == "2026-03-02T10:00:00"


def test_parse_all_day_invalid_end_and_duration() -> None:
    bad_end = parse_ics(
        event(
            "UID:bad-end",
            "DTSTART;VALUE=DATE:20260302",
            "DTEND;VALUE=DATE:bad",
        )
    )
    bad_duration = parse_ics(
        event(
            "UID:bad-duration",
            "DTSTART;VALUE=DATE:20260302",
            "DURATION:nope",
        )
    )

    assert bad_end.events[0].end_date == "2026-03-03"
    assert bad_duration.events[0].end_date == "2026-03-03"
    assert any("invalid DATE" in warning for warning in bad_end.warnings)
    assert any("invalid DURATION" in warning for warning in bad_duration.warnings)


def test_parse_empty_and_invalid_recurrence_values() -> None:
    result = parse_ics(
        event(
            "UID:recurrence",
            "DTSTART:20260302T090000Z",
            "EXDATE:20260316T090000Z,",
            "EXDATE:garbage",
        )
    )

    assert result.events[0].exdate == ("2026-03-16T09:00:00Z",)
    assert any("invalid DATE-TIME" in warning for warning in result.warnings)


def test_parse_organizer_with_non_mailto_address() -> None:
    draft = parse_ics(
        event(
            "UID:organizer",
            "DTSTART:20260302T090000Z",
            "ORGANIZER:urn:uuid:1234",
        )
    ).events[0]

    assert draft.organizer_email == "urn:uuid:1234"


def test_parse_categories_skip_empty_values() -> None:
    draft = parse_ics(
        event(
            "UID:categories",
            "DTSTART:20260302T090000Z",
            "CATEGORIES:work,,design",
        )
    ).events[0]

    assert draft.categories == ("work", "design")


def test_parse_unknown_child_component_in_event_warns() -> None:
    result = parse_ics(
        event(
            "UID:child",
            "DTSTART:20260302T090000Z",
            "BEGIN:VUNKNOWN",
            "END:VUNKNOWN",
        )
    )

    assert any("unsupported component VUNKNOWN" in warning for warning in result.warnings)


def test_parse_invalid_repeat_warns() -> None:
    result = parse_ics(
        event(
            "UID:repeat",
            "DTSTART:20260302T090000Z",
            "BEGIN:VALARM",
            "TRIGGER:-PT10M",
            "REPEAT:many",
            "END:VALARM",
        )
    )

    assert result.events[0].reminders[0].repeat_count == 0
    assert any("invalid REPEAT" in warning for warning in result.warnings)


def test_parse_invalid_dtstamp_warns() -> None:
    result = parse_ics(
        event(
            "UID:stamp",
            "DTSTART:20260302T090000Z",
            "DTSTAMP:nope",
        )
    )

    assert result.events[0].dtstamp_utc is None
    assert any("invalid DATE-TIME" in warning for warning in result.warnings)


def test_parse_unknown_recurrence_range_warns() -> None:
    result = parse_ics(
        event(
            "UID:range",
            "DTSTART:20260302T090000Z",
            "RECURRENCE-ID;RANGE=NOPE:20260302T090000Z",
        )
    )

    assert result.events[0].recurrence_range is None
    assert any("unknown RANGE" in warning for warning in result.warnings)


def test_parse_component_nesting_is_bounded() -> None:
    depth = 40
    text = "".join("BEGIN:VCALENDAR\r\n" for _ in range(depth))
    text += "BEGIN:VEVENT\r\nUID:deep\r\nDTSTART:20260302T090000Z\r\nEND:VEVENT\r\n"
    text += "".join("END:VCALENDAR\r\n" for _ in range(depth))

    result = parse_ics(text)

    assert result.events == []
    assert any("component nesting exceeds" in warning for warning in result.warnings)


def test_parse_pathological_duration_digits_do_not_crash() -> None:
    result = parse_ics(
        event(
            "UID:huge-duration",
            "DTSTART:20260302T090000Z",
            "DURATION:P" + "9" * 5000 + "D",
        )
    )

    assert result.events[0].duration_seconds == 0
    assert any("invalid DURATION" in warning for warning in result.warnings)


def test_parse_vtimezone_is_ignored_without_noise() -> None:
    result = parse_ics(
        wrap(
            "BEGIN:VTIMEZONE",
            "TZID:America/New_York",
            "END:VTIMEZONE",
            "BEGIN:VEVENT",
            "UID:vtz",
            "DTSTART;TZID=America/New_York:20260302T090000",
            "END:VEVENT",
        )
    )

    assert result.events[0].start_tz == "America/New_York"
    assert result.warnings == []


def test_parse_unmodeled_alarm_property_warns() -> None:
    result = parse_ics(
        event(
            "UID:alarm-extras",
            "DTSTART:20260302T090000Z",
            "BEGIN:VALARM",
            "TRIGGER:-PT10M",
            "X-ALARM-EXTRA:value",
            "ACKNOWLEDGED:20260302T080000Z",
            "END:VALARM",
        )
    )

    alarms = result.events[0].reminders
    assert len(alarms) == 1
    assert any("X-ALARM-EXTRA" in warning for warning in result.warnings)
    assert any("ACKNOWLEDGED" in warning for warning in result.warnings)
