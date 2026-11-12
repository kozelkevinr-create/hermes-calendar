"""Canonical event model and value objects.

Owned by the core design (``docs/design/core.md``). Types here are storage- and
transport-agnostic; sync and UI layers map into them.
"""

from __future__ import annotations

from hermes_calendar.models.calendar import Calendar, Source
from hermes_calendar.models.event import Attendee, Event, EventDraft, Reminder
from hermes_calendar.models.ids import new_uuid7

__all__ = [
    "Attendee",
    "Calendar",
    "Event",
    "EventDraft",
    "Reminder",
    "Source",
    "new_uuid7",
]
