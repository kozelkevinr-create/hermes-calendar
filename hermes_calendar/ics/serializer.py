"""RFC 5545 writer: :class:`~hermes_calendar.models.EventDraft` to iCalendar text.

Output is deterministic CRLF text with folded lines and a minimal
``VCALENDAR`` wrapper. The writer is one-way by design (sync.md §3.5): it never
emits ``METHOD``, attendee responses, or feed credentials.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import Attendee, EventDraft, Reminder
from ._common import (
    PRODID,
    clean_line_value,
    escape_text,
    fold_line,
    format_duration,
    format_local,
    format_utc,
)

_DEFAULT_ROLE = "REQ-PARTICIPANT"
_DEFAULT_PARTSTAT = "NEEDS-ACTION"


def serialize_events(events: Iterable[EventDraft]) -> str:
    """Serialize drafts as a CRLF-delimited ``VCALENDAR`` document.

    ``DTEND`` is always written, even when the draft came from a ``DURATION``
    property, because the model normalizes durations to instants (core.md
    Appendix A). Recurrence values are expected in the canonical ISO form
    produced by :func:`hermes_calendar.ics.parse_ics`.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(_event_lines(event))
    lines.append("END:VCALENDAR")
    return "".join(f"{fold_line(line)}\r\n" for line in lines)


def _event_lines(event: EventDraft) -> list[str]:
    lines = ["BEGIN:VEVENT"]
    lines.append(f"UID:{clean_line_value(event.uid or _synthetic_uid(event))}")
    if event.dtstamp_utc is not None:
        lines.append(f"DTSTAMP:{format_utc(event.dtstamp_utc)}")
    lines.append(f"SUMMARY:{escape_text(event.summary)}")
    if event.description is not None:
        lines.append(f"DESCRIPTION:{escape_text(event.description)}")
    if event.location is not None:
        lines.append(f"LOCATION:{escape_text(event.location)}")
    lines.extend(_range_lines(event))

    if event.rrule:
        lines.append(f"RRULE:{clean_line_value(event.rrule)}")
    lines.extend(_recurrence_lines("EXDATE", event.exdate, event))
    lines.extend(_recurrence_lines("RDATE", event.rdate, event))
    recurrence = _recurrence_id_line(event)
    if recurrence is not None:
        lines.append(recurrence)
    if event.status != "CONFIRMED":
        lines.append(f"STATUS:{event.status}")
    if event.transparency != "OPAQUE":
        lines.append(f"TRANSP:{event.transparency}")
    if event.classification != "PUBLIC":
        lines.append(f"CLASS:{event.classification}")
    if event.sequence:
        lines.append(f"SEQUENCE:{event.sequence}")
    organizer = _organizer_line(event)
    if organizer is not None:
        lines.append(organizer)
    lines.extend(_attendee_lines(event.attendees))
    if event.categories:
        lines.append("CATEGORIES:" + ",".join(escape_text(value) for value in event.categories))
    if event.url is not None:
        lines.append(f"URL:{clean_line_value(event.url)}")
    if event.created_utc is not None:
        lines.append(f"CREATED:{format_utc(event.created_utc)}")
    if event.last_modified_utc is not None:
        lines.append(f"LAST-MODIFIED:{format_utc(event.last_modified_utc)}")

    lines.extend(_xprop_lines(event.xprops))
    for reminder in event.reminders:
        lines.extend(_alarm_lines(reminder))
    lines.append("END:VEVENT")
    return lines


def _range_lines(event: EventDraft) -> list[str]:
    if event.is_all_day:
        start_date = event.start_date or _date_from_epoch(event.start_utc)
        end_date = event.end_date or _add_days(start_date, 1)
        return [
            f"DTSTART;VALUE=DATE:{_compact_date(start_date)}",
            f"DTEND;VALUE=DATE:{_compact_date(end_date)}",
        ]
    start_local = _wall_clock(event.start_tz, event.start_utc, event.start_local)
    lines = [_date_time_line("DTSTART", event.start_tz, event.start_utc, start_local)]
    if event.start_tz == "UTC":
        end_value = format_utc(event.end_utc)
        lines.append(f"DTEND:{end_value}")
    else:
        end_local = _wall_clock(event.end_tz or event.start_tz, event.end_utc, event.end_local)
        lines.append(
            _date_time_line("DTEND", event.end_tz or event.start_tz, event.end_utc, end_local)
        )
    return lines


def _date_time_line(name: str, tzid: str | None, epoch: int, local: str) -> str:
    if tzid == "UTC":
        return f"{name}:{format_utc(epoch)}"
    if tzid:
        return f"{name};TZID={_param(tzid)}:{local}"
    return f"{name}:{local}"


def _wall_clock(tzid: str | None, epoch: int, stored: str | None) -> str:
    if stored:
        return _compact_local(stored)
    if tzid and tzid != "UTC":
        try:
            instant = datetime.fromtimestamp(epoch, ZoneInfo(tzid))
        except (ZoneInfoNotFoundError, ValueError):
            instant = datetime.fromtimestamp(epoch, UTC)
        return format_local(instant.replace(tzinfo=None))
    return format_local(datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None))


def _recurrence_lines(name: str, values: tuple[str, ...] | None, event: EventDraft) -> list[str]:
    if not values:
        return []
    groups: dict[str, list[str]] = {}
    for value in values:
        params, formatted = _recurrence_token(value, event)
        groups.setdefault(params, []).append(formatted)
    return [f"{name}{params}:{','.join(items)}" for params, items in groups.items()]


def _recurrence_token(value: str, event: EventDraft) -> tuple[str, str]:
    if event.is_all_day:
        return ";VALUE=DATE", _compact_date(value[:10])
    if value.upper().endswith("Z"):
        return "", value[:-1].replace("-", "").replace(":", "") + "Z"
    if event.start_tz == "UTC":
        return "", value.replace("-", "").replace(":", "") + "Z"
    if event.start_tz:
        return f";TZID={_param(event.start_tz)}", value.replace("-", "").replace(":", "")
    return "", value.replace("-", "").replace(":", "")


def _recurrence_id_line(event: EventDraft) -> str | None:
    if event.recurrence_id_raw is None:
        return None
    params, value = _recurrence_token(event.recurrence_id_raw, event)
    if event.recurrence_range:
        params += f";RANGE={event.recurrence_range}"
    return f"RECURRENCE-ID{params}:{value}"


def _organizer_line(event: EventDraft) -> str | None:
    if event.organizer_email is None and event.organizer_name is None:
        return None
    params = ""
    if event.organizer_name is not None:
        params += f";CN={_param(event.organizer_name)}"
    address = ""
    if event.organizer_email is not None:
        email = clean_line_value(event.organizer_email)
        address = email if ":" in email else f"mailto:{email}"
    return f"ORGANIZER{params}:{address}"


def _attendee_lines(attendees: tuple[Attendee, ...]) -> list[str]:
    lines: list[str] = []
    for attendee in attendees:
        params: list[str] = []
        if attendee.name is not None:
            params.append(f"CN={_param(attendee.name)}")
        if attendee.role != _DEFAULT_ROLE:
            params.append(f"ROLE={attendee.role}")
        if attendee.partstat != _DEFAULT_PARTSTAT:
            params.append(f"PARTSTAT={attendee.partstat}")
        if attendee.rsvp:
            params.append("RSVP=TRUE")
        if attendee.cutype is not None:
            params.append(f"CUTYPE={attendee.cutype}")
        if attendee.delegated_to is not None:
            params.append(f"DELEGATED-TO={_param(attendee.delegated_to)}")
        if attendee.delegated_from is not None:
            params.append(f"DELEGATED-FROM={_param(attendee.delegated_from)}")
        if attendee.sent_by is not None:
            params.append(f"SENT-BY={_param(attendee.sent_by)}")
        suffix = ";" + ";".join(params) if params else ""
        lines.append(f"ATTENDEE{suffix}:{clean_line_value(attendee.address)}")
    return lines


def _xprop_lines(xprops: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for value in xprops.values():
        values = value if isinstance(value, list | tuple) else [value]
        for item in values:
            line = clean_line_value(str(item))
            if line:
                lines.append(line)
    return lines


def _alarm_lines(reminder: Reminder) -> list[str]:
    lines = ["BEGIN:VALARM", f"ACTION:{reminder.action}"]
    if reminder.trigger_kind.upper() == "ABSOLUTE" and reminder.absolute_utc is not None:
        lines.append(f"TRIGGER;VALUE=DATE-TIME:{format_utc(reminder.absolute_utc)}")
    else:
        related = ";RELATED=END" if reminder.related == "END" else ""
        lines.append(f"TRIGGER{related}:{format_duration(reminder.offset_seconds or 0)}")
    if reminder.description is not None:
        lines.append(f"DESCRIPTION:{escape_text(reminder.description)}")
    if reminder.repeat_count > 0 and reminder.repeat_interval_seconds:
        lines.append(f"REPEAT:{reminder.repeat_count}")
        lines.append(f"DURATION:{format_duration(reminder.repeat_interval_seconds)}")
    lines.append("END:VALARM")
    return lines


def _synthetic_uid(event: EventDraft) -> str:
    seed = f"{event.calendar_id}|{event.start_utc}|{event.end_utc}|{event.summary}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{digest}@hermes-calendar.invalid"


def _param(value: str) -> str:
    cleaned = clean_line_value(value)
    if any(char in cleaned for char in ':;,"'):
        return '"' + cleaned.replace('"', "'") + '"'
    return cleaned


def _date_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).date().isoformat()


def _add_days(value: str, days: int) -> str:
    day = datetime.strptime(value[:10], "%Y-%m-%d").date()
    return (day + timedelta(days=days)).isoformat()


def _compact_date(value: str) -> str:
    return value[:10].replace("-", "")


def _compact_local(value: str) -> str:
    return value.replace("-", "").replace(":", "")
