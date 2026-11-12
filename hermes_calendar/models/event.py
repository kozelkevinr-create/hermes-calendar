"""Canonical event aggregate models.

``EventDraft`` is the write-side input: it carries only values a caller may
choose. The repository mints identifiers, scope, ownership, revisions, and
bookkeeping timestamps. ``Event`` is the stored aggregate, including its
attendees and reminder definitions.

Times are UTC epoch seconds; local wall times and IANA zone names are retained
only for recurrence and round-trip (``core.md`` §3.4). All-day events use
exclusive ``end_date`` values and never use the wall-clock columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_STATUS = "CONFIRMED"
DEFAULT_TRANSPARENCY = "OPAQUE"
DEFAULT_CLASSIFICATION = "PUBLIC"
DEFAULT_ROLE = "REQ-PARTICIPANT"
DEFAULT_PARTSTAT = "NEEDS-ACTION"
DEFAULT_ACTION = "DISPLAY"
DEFAULT_RELATED = "START"
DEFAULT_TRIGGER_KIND = "RELATIVE"


@dataclass(frozen=True)
class Attendee:
    """One ``ATTENDEE`` row. Order is preserved by the repository."""

    address: str
    email: str | None = None
    name: str | None = None
    role: str = DEFAULT_ROLE
    partstat: str = DEFAULT_PARTSTAT
    rsvp: bool = False
    cutype: str | None = None
    delegated_to: str | None = None
    delegated_from: str | None = None
    sent_by: str | None = None


@dataclass(frozen=True)
class Reminder:
    """One ``VALARM`` definition (delivery state is notification-owned)."""

    action: str = DEFAULT_ACTION
    related: str = DEFAULT_RELATED
    trigger_kind: str = DEFAULT_TRIGGER_KIND
    offset_seconds: int | None = None
    absolute_utc: int | None = None
    repeat_count: int = 0
    repeat_interval_seconds: int | None = None
    description: str | None = None
    id: str | None = None


@dataclass(frozen=True, kw_only=True)
class EventDraft:
    """Caller-supplied values for :meth:`Repository.create_event`.

    ``source_id`` is deliberately absent: it is always taken from the target
    calendar, which is also how the composite foreign key keeps an event from
    disagreeing with its calendar.
    """

    calendar_id: str
    start_utc: int
    end_utc: int

    summary: str = ""
    description: str | None = None
    location: str | None = None
    url: str | None = None

    start_local: str | None = None
    start_tz: str | None = None
    end_local: str | None = None
    end_tz: str | None = None
    duration_seconds: int | None = None
    start_date: str | None = None
    end_date: str | None = None

    is_all_day: bool = False
    is_floating: bool = False

    id: str | None = None
    uid: str | None = None
    owner_profile_id: str | None = None
    remote_href: str | None = None
    etag: str | None = None
    sequence: int = 0

    rrule: str | None = None
    rrule_unsupported: tuple[str, ...] | None = None
    rdate: tuple[str, ...] | None = None
    exdate: tuple[str, ...] | None = None
    parent_id: str | None = None
    recurrence_key: str | None = None
    recurrence_id_raw: str | None = None
    recurrence_range: str | None = None

    status: str = DEFAULT_STATUS
    transparency: str = DEFAULT_TRANSPARENCY
    classification: str = DEFAULT_CLASSIFICATION
    organizer_email: str | None = None
    organizer_name: str | None = None
    categories: tuple[str, ...] = ()
    xprops: dict[str, Any] = field(default_factory=dict)

    created_utc: int | None = None
    dtstamp_utc: int | None = None
    last_modified_utc: int | None = None

    attendees: tuple[Attendee, ...] = ()
    reminders: tuple[Reminder, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Event:
    """The stored event aggregate.

    One row per master or override, plus ordered attendee and reminder rows.
    ``rev`` is the local revision and compare-and-swap token; child-table
    triggers increment it, so a create with children may return ``rev > 1``.
    """

    id: str
    calendar_id: str
    source_id: str
    scope: str
    uid: str
    rev: int
    summary: str
    start_utc: int
    end_utc: int
    updated_utc: int

    owner_profile_id: str | None = None
    remote_href: str | None = None
    etag: str | None = None
    sequence: int = 0

    description: str | None = None
    location: str | None = None
    url: str | None = None

    start_local: str | None = None
    start_tz: str | None = None
    end_local: str | None = None
    end_tz: str | None = None
    duration_seconds: int | None = None
    start_date: str | None = None
    end_date: str | None = None

    is_all_day: bool = False
    is_floating: bool = False

    rrule: str | None = None
    rrule_unsupported: tuple[str, ...] | None = None
    rdate: tuple[str, ...] | None = None
    exdate: tuple[str, ...] | None = None
    parent_id: str | None = None
    recurrence_key: str | None = None
    recurrence_id_raw: str | None = None
    recurrence_range: str | None = None

    status: str = DEFAULT_STATUS
    transparency: str = DEFAULT_TRANSPARENCY
    classification: str = DEFAULT_CLASSIFICATION
    organizer_email: str | None = None
    organizer_name: str | None = None
    categories: tuple[str, ...] = ()
    xprops: dict[str, Any] = field(default_factory=dict)

    created_utc: int | None = None
    dtstamp_utc: int | None = None
    last_modified_utc: int | None = None
    deleted_utc: int | None = None

    attendees: tuple[Attendee, ...] = ()
    reminders: tuple[Reminder, ...] = ()
