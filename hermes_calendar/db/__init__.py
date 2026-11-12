"""SQLite storage for the calendar plugin.

The database is the aggregate root described in ``docs/design/core.md`` (T2).
Only this package opens connections; callers receive a configured
``sqlite3.Connection`` and must not set pragmas themselves.
"""

from __future__ import annotations

from hermes_calendar.db.connection import connect

__all__ = ["connect"]
