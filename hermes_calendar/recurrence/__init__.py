"""RFC 5545 recurrence expansion.

Expansion happens in local wall-clock time using the event's IANA zone; see
``docs/design/core.md`` §6. Keep timezone-aware instant math in this package,
not in callers.

The package is pure: it has no database or Hermes imports. Callers map storage
rows into :class:`MasterEvent` / :class:`OverrideEvent` and receive
:class:`Occurrence` values. Rule parts outside the P0 subset raise
:class:`UnsupportedRecurrence`; they are never silently ignored.
"""

from __future__ import annotations

from .expand import ExpansionLimitExceeded, expand
from .rule import UnsupportedRecurrence
from .types import (
    MasterEvent,
    Occurrence,
    OverrideEvent,
    all_day_recurrence_key,
    timed_recurrence_key,
)

__all__ = [
    "ExpansionLimitExceeded",
    "MasterEvent",
    "Occurrence",
    "OverrideEvent",
    "UnsupportedRecurrence",
    "all_day_recurrence_key",
    "expand",
    "timed_recurrence_key",
]
