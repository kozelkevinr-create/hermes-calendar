"""iCalendar (RFC 5545) import and export for ``VEVENT`` calendars.

This package is a pure-Python, dependency-free bridge between calendar text
and the canonical event model. It is deliberately library-only: it does not
fetch URLs, register Hermes tools, or touch the database.

Public surface:

* :func:`parse_ics` — unfold, parse ``VEVENT``/``VALARM``, apply the hard size
  and event-count limits, and return drafts plus warnings.
* :func:`serialize_events` — emit deterministic CRLF iCalendar text.
* :class:`ParseResult`, :class:`IcsLimitError`, :data:`MAX_ICS_BYTES`,
  :data:`MAX_EVENTS`.

ICS is one-way (``docs/design/sync.md`` §3.5): there is no writeback path.
"""

from __future__ import annotations

from ._common import MAX_EVENTS, MAX_ICS_BYTES, IcsLimitError
from .parser import ParseResult, parse_ics
from .serializer import serialize_events

__all__ = [
    "MAX_EVENTS",
    "MAX_ICS_BYTES",
    "IcsLimitError",
    "ParseResult",
    "parse_ics",
    "serialize_events",
]
