"""Canonical event model and value objects.

Owned by the core design (``docs/design/core.md``). Types here are storage- and
transport-agnostic; sync and UI layers map into them.
"""

from __future__ import annotations

from .calendar import Calendar, Source
from .event import Attendee, Event, EventDraft, Reminder
from .ids import new_uuid7

__all__ = [
    "Attendee",
    "Calendar",
    "Event",
    "EventDraft",
    "Reminder",
    "Source",
    "new_uuid7",
]
