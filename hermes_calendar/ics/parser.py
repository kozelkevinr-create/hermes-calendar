"""RFC 5545 reader: ``VEVENT``/``VALARM`` to :class:`~hermes_calendar.models.EventDraft`.

The parser is intentionally forgiving about malformed input: a bad property or
component produces a warning and is skipped, never an exception. The only
hard failures are limit violations (:class:`IcsLimitError`) and programmer
errors such as passing a non-string.

Parsed drafts are store-agnostic: ``calendar_id`` is ``""`` and overrides carry
``recurrence_key``/``recurrence_id_raw`` but no ``parent_id`` (the master's
local id is minted when the caller writes the series). Callers bind drafts with
``dataclasses.replace``.

Time-zone handling uses the standard-library ``zoneinfo`` database. RFC 5545
DST rules are implemented by ``datetime.replace(tzinfo=zone)`` with ``fold=0``:
ambiguous wall times take the first (pre-transition) offset and nonexistent
times land after the gap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hermes_calendar.ics._common import (
    MAX_EVENTS,
    MAX_ICS_BYTES,
    IcsLimitError,
    parse_duration,
    split_content_line,
    split_escaped,
    split_outside_quotes,
    unescape_text,
    unquote,
)
from hermes_calendar.models import Attendee, EventDraft, Reminder

_DEFAULT_ROLE = "REQ-PARTICIPANT"
_DEFAULT_PARTSTAT = "NEEDS-ACTION"

_STATUSES = frozenset({"TENTATIVE", "CONFIRMED", "CANCELLED"})
_TRANSPARENCIES = frozenset({"OPAQUE", "TRANSPARENT"})
_CLASSIFICATIONS = frozenset({"PUBLIC", "PRIVATE", "CONFIDENTIAL"})
_ROLES = frozenset({"REQ-PARTICIPANT", "OPT-PARTICIPANT", "NON-PARTICIPANT", "CHAIR"})
_PARTSTATS = frozenset({"NEEDS-ACTION", "ACCEPTED", "DECLINED", "TENTATIVE", "DELEGATED"})
_CUTYPES = frozenset({"INDIVIDUAL", "GROUP", "RESOURCE", "ROOM", "UNKNOWN"})
_ALARM_ACTIONS = frozenset({"DISPLAY", "AUDIO", "EMAIL"})
_RELATED = frozenset({"START", "END"})
_RANGES = frozenset({"THIS", "THISANDFUTURE"})

#: RRULE parts expanded by the v1 recurrence engine (core.md §6.2).
_RRULE_SUPPORTED = frozenset(
    {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}
)

_CALENDAR_PROPERTIES = frozenset({"VERSION", "PRODID", "CALSCALE", "METHOD", "REFRESH-INTERVAL"})

_MODELED_PROPERTIES = frozenset(
    {
        "UID",
        "SUMMARY",
        "DESCRIPTION",
        "LOCATION",
        "DTSTART",
        "DTEND",
        "DURATION",
        "RRULE",
        "EXDATE",
        "RDATE",
        "RECURRENCE-ID",
        "STATUS",
        "TRANSP",
        "CLASS",
        "SEQUENCE",
        "ORGANIZER",
        "ATTENDEE",
        "CATEGORIES",
        "URL",
        "CREATED",
        "LAST-MODIFIED",
        "DTSTAMP",
    }
)

_IGNORED_COMPONENTS = frozenset({"VTODO", "VJOURNAL", "VFREEBUSY", "VAVAILABILITY"})
_SILENT_COMPONENTS = frozenset({"VTIMEZONE"})
_ALARM_PROPERTIES = frozenset({"ACTION", "TRIGGER", "DESCRIPTION", "REPEAT", "DURATION"})

#: Bound on component nesting so a hostile document cannot exhaust the stack.
_MAX_COMPONENT_DEPTH = 32


@dataclass
class ParseResult:
    """Outcome of :func:`parse_ics`.

    ``warnings`` describes every component, property, or value that could not
    be represented faithfully. An empty ``warnings`` list means the document
    parsed without losses we know about. Both collections are ordinary lists so
    callers can extend or replace drafts in place.
    """

    events: list[EventDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Value:
    kind: Literal["date", "utc", "floating", "zoned"]
    day: date | None
    local: datetime | None
    tzid: str | None
    utc: int


@dataclass(frozen=True)
class _ContentLine:
    name: str
    params: dict[str, str]
    value: str
    raw: str


@dataclass
class _Component:
    name: str
    props: dict[str, list[_ContentLine]] = field(default_factory=dict)
    children: list[_Component] = field(default_factory=list)

    def first(self, name: str) -> _ContentLine | None:
        values = self.props.get(name)
        return values[0] if values else None

    def every(self, name: str) -> list[_ContentLine]:
        return self.props.get(name, [])


def parse_ics(
    text: str,
    *,
    max_bytes: int = MAX_ICS_BYTES,
    max_events: int = MAX_EVENTS,
) -> ParseResult:
    """Parse an iCalendar document into event drafts.

    ``max_bytes`` is checked against the UTF-8 byte length before any parsing.
    Once ``max_events`` drafts have been produced the remainder are skipped and
    one warning is recorded. Neither limit is a security boundary on its own:
    callers must still avoid fetching untrusted URLs in-process.
    """
    if not isinstance(text, str):
        raise TypeError("parse_ics expects str input")
    if len(text.encode("utf-8")) > max_bytes:
        raise IcsLimitError(f"iCalendar document exceeds {max_bytes} bytes")

    warnings: list[str] = []
    roots = _build_components(_unfold(text), warnings)

    containers = [component for component in roots if component.name == "VCALENDAR"]
    loose = [component for component in roots if component.name != "VCALENDAR"]
    if containers:
        if loose:
            warnings.append("components outside VCALENDAR were still parsed")
            containers.append(_Component("VCALENDAR", children=loose))
    else:
        warnings.append("document has no VCALENDAR component")
        containers = [_Component("VCALENDAR", children=roots)]

    events: list[EventDraft] = []
    capped = [False]
    for container in containers:
        _warn_calendar_properties(container, warnings)
        _walk(container, warnings, events, max_events, capped)
    return ParseResult(events=events, warnings=warnings)


def _unfold(text: str) -> list[str]:
    """Unfold RFC 5545 continuation lines and drop blank lines."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _parse_content_line(line: str, warnings: list[str]) -> _ContentLine | None:
    left, value = split_content_line(line)
    if not left:
        warnings.append(f"ignoring line without a property name: {line!r}")
        return None
    parts = split_outside_quotes(left, ";")
    name = parts[0].strip().upper()
    if not name:
        warnings.append(f"ignoring line without a property name: {line!r}")
        return None
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            warnings.append(f"{name}: ignoring parameter without '=': {part!r}")
            continue
        key, raw_value = part.split("=", 1)
        params[key.strip().upper()] = unquote(raw_value.strip())
    return _ContentLine(name=name, params=params, value=value, raw=line)


def _build_components(lines: list[str], warnings: list[str]) -> list[_Component]:
    root = _Component("ROOT")
    stack = [root]
    ignored_depth = 0
    for line in lines:
        parsed = _parse_content_line(line, warnings)
        if parsed is None:
            continue
        if ignored_depth:
            if parsed.name == "BEGIN":
                ignored_depth += 1
            elif parsed.name == "END":
                ignored_depth -= 1
            continue
        if parsed.name == "BEGIN":
            if len(stack) >= _MAX_COMPONENT_DEPTH:
                warnings.append(
                    f"component nesting exceeds {_MAX_COMPONENT_DEPTH}; "
                    f"BEGIN:{parsed.value.strip().upper()} ignored"
                )
                ignored_depth = 1
                continue
            stack.append(_Component(parsed.value.strip().upper()))
        elif parsed.name == "END":
            name = parsed.value.strip().upper()
            if len(stack) > 1 and stack[-1].name == name:
                completed = stack.pop()
                stack[-1].children.append(completed)
            else:
                warnings.append(f"unexpected END:{name}")
        else:
            stack[-1].props.setdefault(parsed.name, []).append(parsed)
    if len(stack) > 1:
        warnings.append(f"unterminated component BEGIN:{stack[-1].name}")
    return root.children


def _warn_calendar_properties(calendar: _Component, warnings: list[str]) -> None:
    for name in calendar.props:
        if name not in _CALENDAR_PROPERTIES:
            warnings.append(f"VCALENDAR property {name} is not exported")


def _walk(
    component: _Component,
    warnings: list[str],
    events: list[EventDraft],
    max_events: int,
    capped: list[bool],
) -> None:
    for child in component.children:
        if child.name == "VEVENT":
            if len(events) >= max_events:
                if not capped[0]:
                    warnings.append(f"event count exceeds {max_events}; extra events skipped")
                    capped[0] = True
                continue
            draft = _convert_event(child, warnings, len(events))
            if draft is not None:
                events.append(draft)
        elif child.name == "VCALENDAR":
            _warn_calendar_properties(child, warnings)
            _walk(child, warnings, events, max_events, capped)
        elif child.name in _IGNORED_COMPONENTS:
            warnings.append(f"ignoring {child.name} component")
        elif child.name in _SILENT_COMPONENTS:
            # VTIMEZONE definitions are redundant when the TZID is in the IANA
            # database; an unknown TZID warns when the VEVENT uses it.
            continue
        else:
            warnings.append(f"ignoring unsupported component {child.name}")


def _parse_dt(prop: _ContentLine, warnings: list[str], label: str) -> _Value | None:
    return _parse_value(prop.value, prop.params, warnings, label)


def _parse_value(
    raw: str, params: dict[str, str], warnings: list[str], label: str
) -> _Value | None:
    value = raw.strip()
    if not value:
        warnings.append(f"{label}: empty value")
        return None
    value_type = params.get("VALUE", "").upper()
    if value_type == "DATE" or (len(value) == 8 and "T" not in value):
        try:
            day = datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            warnings.append(f"{label}: invalid DATE {value!r}")
            return None
        return _Value("date", day, None, None, _date_epoch(day))

    utc_suffix = value.endswith(("Z", "z"))
    body = value[:-1] if utc_suffix else value
    try:
        if len(body) == 15 and body[8] == "T":
            local = datetime.strptime(body, "%Y%m%dT%H%M%S")
        elif len(body) == 13 and body[8] == "T":
            local = datetime.strptime(body + "00", "%Y%m%dT%H%M%S")
        else:
            local = datetime.fromisoformat(body)
    except ValueError:
        warnings.append(f"{label}: invalid DATE-TIME {value!r}")
        return None

    if local.tzinfo is not None:
        instant = local.astimezone(UTC)
        return _Value("utc", None, instant.replace(tzinfo=None), "UTC", int(instant.timestamp()))
    if utc_suffix:
        return _Value("utc", None, local, "UTC", int(local.replace(tzinfo=UTC).timestamp()))

    tzid = params.get("TZID")
    if tzid:
        try:
            zone = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError):
            warnings.append(f"{label}: unknown TZID {tzid!r}; treating as UTC")
            return _Value("utc", None, local, "UTC", int(local.replace(tzinfo=UTC).timestamp()))
        return _Value("zoned", None, local, tzid, int(local.replace(tzinfo=zone).timestamp()))
    return _Value("floating", None, local, None, int(local.replace(tzinfo=UTC).timestamp()))


def _date_epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def _wall_from_epoch(start: _Value, epoch: int) -> datetime:
    assert start.local is not None
    if start.kind == "zoned" and start.tzid is not None:
        return datetime.fromtimestamp(epoch, ZoneInfo(start.tzid)).replace(tzinfo=None)
    if start.kind == "utc":
        return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)
    return start.local + timedelta(seconds=epoch - start.utc)


def _resolve_instant(value: _Value, start: _Value) -> int:
    if value.kind in ("utc", "zoned"):
        return value.utc
    assert value.local is not None
    if start.kind == "zoned" and start.tzid is not None:
        zone = ZoneInfo(start.tzid)
        return int(value.local.replace(tzinfo=zone).timestamp())
    return value.utc


def _parse_instant(comp: _Component, name: str, warnings: list[str], label: str) -> int | None:
    prop = comp.first(name)
    if prop is None:
        return None
    parsed = _parse_dt(prop, warnings, f"{label}: {name}")
    if parsed is None:
        return None
    if parsed.kind == "date":
        warnings.append(f"{label}: {name} must be a DATE-TIME; ignored")
        return None
    return parsed.utc


def _convert_event(comp: _Component, warnings: list[str], index: int) -> EventDraft | None:
    uid_prop = comp.first("UID")
    uid = uid_prop.value.strip() if uid_prop is not None else ""
    if not uid:
        uid = _synthetic_uid(comp, index)
        warnings.append(f"VEVENT {index}: missing UID; generated {uid}")
    label = f"VEVENT {uid}"

    start_prop = comp.first("DTSTART")
    if start_prop is None:
        warnings.append(f"{label}: missing DTSTART; event skipped")
        return None
    start = _parse_dt(start_prop, warnings, f"{label}: DTSTART")
    if start is None:
        warnings.append(f"{label}: unusable DTSTART; event skipped")
        return None

    end_prop = comp.first("DTEND")
    duration_prop = comp.first("DURATION")
    if end_prop is not None and duration_prop is not None:
        warnings.append(f"{label}: both DTEND and DURATION; using DTEND")

    is_all_day = start.kind == "date"
    start_utc, end_utc, end_local, end_date, duration_seconds = _resolve_range(
        comp, start, end_prop, duration_prop, warnings, label
    )

    recurrence_id_prop = comp.first("RECURRENCE-ID")
    recurrence_id_raw: str | None = None
    recurrence_key: str | None = None
    recurrence_range: str | None = None
    if recurrence_id_prop is not None:
        recurrence_id_raw = _recurrence_value(
            recurrence_id_prop.value,
            recurrence_id_prop.params,
            warnings,
            f"{label}: RECURRENCE-ID",
        )
        parsed_recurrence = _parse_dt(recurrence_id_prop, warnings, f"{label}: RECURRENCE-ID")
        if parsed_recurrence is not None:
            if parsed_recurrence.kind == "date" and parsed_recurrence.day is not None:
                recurrence_key = f"D:{parsed_recurrence.day.isoformat()}"
            else:
                recurrence_key = f"T:{parsed_recurrence.utc}"
        raw_range = recurrence_id_prop.params.get("RANGE", "").upper()
        if raw_range:
            if raw_range in _RANGES:
                recurrence_range = raw_range
            else:
                warnings.append(f"{label}: unknown RANGE {raw_range!r}; ignored")

    rrule: str | None = None
    rrule_unsupported: tuple[str, ...] | None = None
    rrule_prop = comp.first("RRULE")
    if rrule_prop is not None:
        rrule = rrule_prop.value.strip() or None
        if rrule is None:
            warnings.append(f"{label}: empty RRULE; ignored")
        elif recurrence_key is not None:
            warnings.append(f"{label}: override carries RRULE; dropped")
            rrule = None
        else:
            rrule_unsupported = _unsupported_rrule(rrule)

    rdate = _recurrence_values(comp, "RDATE", warnings, label)
    exdate = _recurrence_values(comp, "EXDATE", warnings, label)

    xprops: dict[str, Any] = {}
    for name, props in comp.props.items():
        if name in _MODELED_PROPERTIES:
            continue
        raws = [prop.raw for prop in props]
        xprops[name] = raws[0] if len(raws) == 1 else raws

    organizer_email, organizer_name = _organizer(comp, warnings, label)
    attendees = _attendees(comp, warnings, label)
    categories = _categories(comp)
    reminders = _reminders(comp, warnings, label)

    summary = _text(comp, "SUMMARY") or ""
    return EventDraft(
        calendar_id="",
        start_utc=start_utc,
        end_utc=end_utc,
        summary=summary,
        description=_text(comp, "DESCRIPTION"),
        location=_text(comp, "LOCATION"),
        url=_raw(comp, "URL"),
        start_local=_format_naive(start.local),
        start_tz=start.tzid,
        end_local=_format_naive(end_local),
        end_tz=start.tzid,
        duration_seconds=duration_seconds,
        start_date=start.day.isoformat() if start.day is not None else None,
        end_date=end_date.isoformat() if end_date is not None else None,
        is_all_day=is_all_day,
        is_floating=start.kind == "floating",
        uid=uid,
        rrule=rrule,
        rrule_unsupported=rrule_unsupported,
        rdate=rdate,
        exdate=exdate,
        recurrence_key=recurrence_key,
        recurrence_id_raw=recurrence_id_raw,
        recurrence_range=recurrence_range,
        status=_enum(comp, "STATUS", _STATUSES, "CONFIRMED", warnings, label),
        transparency=_enum(comp, "TRANSP", _TRANSPARENCIES, "OPAQUE", warnings, label),
        classification=_enum(comp, "CLASS", _CLASSIFICATIONS, "PUBLIC", warnings, label),
        organizer_email=organizer_email,
        organizer_name=organizer_name,
        categories=categories,
        xprops=xprops,
        created_utc=_parse_instant(comp, "CREATED", warnings, label),
        dtstamp_utc=_parse_instant(comp, "DTSTAMP", warnings, label),
        last_modified_utc=_parse_instant(comp, "LAST-MODIFIED", warnings, label),
        sequence=_sequence(comp, warnings, label),
        attendees=attendees,
        reminders=reminders,
    )


def _resolve_range(
    comp: _Component,
    start: _Value,
    end_prop: _ContentLine | None,
    duration_prop: _ContentLine | None,
    warnings: list[str],
    label: str,
) -> tuple[int, int, datetime | None, date | None, int | None]:
    """Return ``(start_utc, end_utc, end_local, end_date, duration_seconds)``."""
    if start.kind == "date":
        assert start.day is not None
        end_date = _all_day_end(start.day, end_prop, duration_prop, warnings, label)
        return (
            _date_epoch(start.day),
            _date_epoch(end_date),
            None,
            end_date,
            None,
        )

    assert start.local is not None
    end_utc: int | None = None
    end_local: datetime | None = None
    if end_prop is not None:
        parsed_end = _parse_dt(end_prop, warnings, f"{label}: DTEND")
        if parsed_end is None:
            pass
        elif parsed_end.kind == "date":
            warnings.append(f"{label}: DTEND must be a DATE-TIME; ignored")
        else:
            end_utc = _resolve_instant(parsed_end, start)
            if start.kind == "floating" and parsed_end.kind == "floating":
                end_local = parsed_end.local
            else:
                end_local = _wall_from_epoch(start, end_utc)

    if end_utc is None and duration_prop is not None:
        parsed_duration = parse_duration(duration_prop.value)
        if parsed_duration is None:
            warnings.append(f"{label}: invalid DURATION {duration_prop.value!r}")
        else:
            if parsed_duration < 0:
                warnings.append(f"{label}: negative DURATION; treated as zero")
                parsed_duration = 0
            end_utc = start.utc + parsed_duration
            if start.kind == "floating":
                end_local = start.local + timedelta(seconds=parsed_duration)
            else:
                end_local = _wall_from_epoch(start, end_utc)

    if end_utc is None:
        end_utc = start.utc
        end_local = start.local
    if end_utc < start.utc:
        warnings.append(f"{label}: DTEND before DTSTART; clamped to DTSTART")
        end_utc = start.utc
        end_local = start.local
    return start.utc, end_utc, end_local, None, end_utc - start.utc


def _all_day_end(
    start_day: date,
    end_prop: _ContentLine | None,
    duration_prop: _ContentLine | None,
    warnings: list[str],
    label: str,
) -> date:
    end_date: date | None = None
    if end_prop is not None:
        parsed_end = _parse_dt(end_prop, warnings, f"{label}: DTEND")
        if parsed_end is None:
            pass
        elif parsed_end.kind != "date" or parsed_end.day is None:
            warnings.append(f"{label}: all-day DTEND must be a DATE; ignored")
        else:
            end_date = parsed_end.day

    if end_date is None and duration_prop is not None:
        seconds = parse_duration(duration_prop.value)
        if seconds is None:
            warnings.append(f"{label}: invalid DURATION {duration_prop.value!r}")
        else:
            if seconds <= 0:
                warnings.append(f"{label}: non-positive all-day DURATION; using one day")
                seconds = 86_400
            days = -(-seconds // 86_400)
            if seconds % 86_400:
                warnings.append(f"{label}: all-day DURATION rounded up to whole days")
            end_date = start_day + timedelta(days=days)

    if end_date is None:
        end_date = start_day + timedelta(days=1)
    if end_date <= start_day:
        warnings.append(f"{label}: DTEND is not after DTSTART; using one day")
        end_date = start_day + timedelta(days=1)
    return end_date


def _format_naive(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%S") if value is not None else None


def _text(comp: _Component, name: str) -> str | None:
    prop = comp.first(name)
    return unescape_text(prop.value) if prop is not None else None


def _raw(comp: _Component, name: str) -> str | None:
    prop = comp.first(name)
    return prop.value.strip() if prop is not None else None


def _enum(
    comp: _Component,
    name: str,
    allowed: frozenset[str],
    default: str,
    warnings: list[str],
    label: str,
) -> str:
    prop = comp.first(name)
    if prop is None:
        return default
    value = prop.value.strip().upper()
    if value in allowed:
        return value
    warnings.append(f"{label}: unknown {name} {value!r}; using {default}")
    return default


def _sequence(comp: _Component, warnings: list[str], label: str) -> int:
    prop = comp.first("SEQUENCE")
    if prop is None:
        return 0
    try:
        return int(prop.value.strip())
    except ValueError:
        warnings.append(f"{label}: invalid SEQUENCE {prop.value!r}; using 0")
        return 0


def _unsupported_rrule(value: str) -> tuple[str, ...] | None:
    parts = [part for part in value.split(";") if part.strip()]
    unsupported = tuple(
        part for part in parts if part.split("=", 1)[0].strip().upper() not in _RRULE_SUPPORTED
    )
    return unsupported or None


def _recurrence_value(
    raw: str,
    params: dict[str, str],
    warnings: list[str],
    label: str,
) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if "/" in value:
        warnings.append(f"{label}: PERIOD reduced to its start")
        value = value.split("/", 1)[0]
    parsed = _parse_value(value, params, warnings, label)
    if parsed is None:
        return None
    if parsed.kind == "date" and parsed.day is not None:
        return parsed.day.isoformat()
    assert parsed.local is not None
    if parsed.kind == "utc":
        return parsed.local.strftime("%Y-%m-%dT%H:%M:%SZ")
    return parsed.local.strftime("%Y-%m-%dT%H:%M:%S")


def _recurrence_values(
    comp: _Component, name: str, warnings: list[str], label: str
) -> tuple[str, ...] | None:
    values: list[str] = []
    for prop in comp.every(name):
        for raw in split_escaped(prop.value):
            normalized = _recurrence_value(raw, prop.params, warnings, f"{label}: {name}")
            if normalized is not None:
                values.append(normalized)
    return tuple(values) if values else None


def _organizer(comp: _Component, warnings: list[str], label: str) -> tuple[str | None, str | None]:
    prop = comp.first("ORGANIZER")
    if prop is None:
        return None, None
    address = prop.value.strip()
    email: str | None = None
    if address.lower().startswith("mailto:"):
        email = address[7:] or None
    elif address:
        email = address
    if email is None:
        warnings.append(f"{label}: ORGANIZER has no usable address")
    name = prop.params.get("CN")
    return email, unescape_text(name) if name is not None else None


def _attendees(comp: _Component, warnings: list[str], label: str) -> tuple[Attendee, ...]:
    attendees: list[Attendee] = []
    for prop in comp.every("ATTENDEE"):
        address = prop.value.strip()
        if not address:
            warnings.append(f"{label}: ATTENDEE without an address; ignored")
            continue
        params = prop.params
        email = address[7:] or None if address.lower().startswith("mailto:") else None
        name = params.get("CN")
        cutype: str | None = None
        raw_cutype = params.get("CUTYPE", "").upper()
        if raw_cutype:
            if raw_cutype in _CUTYPES:
                cutype = raw_cutype
            else:
                warnings.append(f"{label}: unknown CUTYPE {raw_cutype!r}; ignored")
        attendees.append(
            Attendee(
                address=address,
                email=email,
                name=unescape_text(name) if name is not None else None,
                role=_param_enum(params, "ROLE", _ROLES, _DEFAULT_ROLE, warnings, label),
                partstat=_param_enum(
                    params, "PARTSTAT", _PARTSTATS, _DEFAULT_PARTSTAT, warnings, label
                ),
                rsvp=params.get("RSVP", "").upper() == "TRUE",
                cutype=cutype,
                delegated_to=params.get("DELEGATED-TO"),
                delegated_from=params.get("DELEGATED-FROM"),
                sent_by=params.get("SENT-BY"),
            )
        )
    return tuple(attendees)


def _param_enum(
    params: dict[str, str],
    name: str,
    allowed: frozenset[str],
    default: str,
    warnings: list[str],
    label: str,
) -> str:
    raw = params.get(name, "").upper()
    if not raw:
        return default
    if raw in allowed:
        return raw
    warnings.append(f"{label}: unknown {name} {raw!r}; using {default}")
    return default


def _categories(comp: _Component) -> tuple[str, ...]:
    values: list[str] = []
    for prop in comp.every("CATEGORIES"):
        for piece in split_escaped(prop.value):
            value = unescape_text(piece)
            if value:
                values.append(value)
    return tuple(values)


def _reminders(comp: _Component, warnings: list[str], label: str) -> tuple[Reminder, ...]:
    reminders: list[Reminder] = []
    for child in comp.children:
        if child.name != "VALARM":
            warnings.append(f"{label}: ignoring unsupported component {child.name}")
            continue
        reminder = _convert_alarm(child, warnings, label)
        if reminder is not None:
            reminders.append(reminder)
    return tuple(reminders)


def _convert_alarm(alarm: _Component, warnings: list[str], event_label: str) -> Reminder | None:
    label = f"{event_label} VALARM"
    for name in alarm.props:
        if name not in _ALARM_PROPERTIES:
            warnings.append(f"{label}: unmodeled property {name} is not exported")
    action = _enum(alarm, "ACTION", _ALARM_ACTIONS, "DISPLAY", warnings, label)
    trigger = alarm.first("TRIGGER")
    if trigger is None:
        warnings.append(f"{label}: missing TRIGGER; ignored")
        return None
    value = trigger.value.strip()
    raw_related = trigger.params.get("RELATED", "START").upper()
    if raw_related not in _RELATED:
        warnings.append(f"{label}: unknown RELATED {raw_related!r}; using START")
        related = "START"
    else:
        related = raw_related

    description = _text(alarm, "DESCRIPTION")
    repeat_count = _positive_int(alarm, "REPEAT", warnings, label) or 0
    repeat_interval: int | None = None
    duration_prop = alarm.first("DURATION")
    if duration_prop is not None:
        parsed_duration = parse_duration(duration_prop.value)
        if parsed_duration is None or parsed_duration <= 0:
            warnings.append(f"{label}: invalid DURATION {duration_prop.value!r}")
        else:
            repeat_interval = parsed_duration
    if repeat_count > 0 and repeat_interval is None:
        warnings.append(f"{label}: REPEAT without a positive DURATION; ignored")
        repeat_count = 0
    for child in alarm.children:
        warnings.append(f"{label}: ignoring unsupported component {child.name}")

    looks_relative = value.lstrip("+-").upper().startswith("P")
    if trigger.params.get("VALUE", "").upper() == "DATE-TIME" or not looks_relative:
        parsed = _parse_dt(trigger, warnings, f"{label}: TRIGGER")
        if parsed is None or parsed.kind == "date":
            warnings.append(f"{label}: unusable absolute TRIGGER; ignored")
            return None
        return Reminder(
            action=action,
            related=related,
            trigger_kind="ABSOLUTE",
            absolute_utc=parsed.utc,
            repeat_count=repeat_count,
            repeat_interval_seconds=repeat_interval,
            description=description,
        )

    offset = parse_duration(value)
    if offset is None:
        warnings.append(f"{label}: invalid relative TRIGGER {value!r}; ignored")
        return None
    return Reminder(
        action=action,
        related=related,
        trigger_kind="RELATIVE",
        offset_seconds=offset,
        repeat_count=repeat_count,
        repeat_interval_seconds=repeat_interval,
        description=description,
    )


def _positive_int(comp: _Component, name: str, warnings: list[str], label: str) -> int | None:
    prop = comp.first(name)
    if prop is None:
        return None
    try:
        value = int(prop.value.strip())
    except ValueError:
        warnings.append(f"{label}: invalid {name} {prop.value!r}; ignored")
        return None
    if value < 0:
        warnings.append(f"{label}: negative {name}; ignored")
        return None
    return value


def _synthetic_uid(comp: _Component, index: int) -> str:
    seed = f"{index}:" + "|".join(
        prop.raw for name in sorted(comp.props) for prop in comp.props[name]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{digest}@hermes-calendar.invalid"
