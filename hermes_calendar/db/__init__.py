"""SQLite storage for the calendar plugin.

The database is the aggregate root described in ``docs/design/core.md`` (T2).
Only this package opens connections; callers receive a configured
``sqlite3.Connection`` and must not set pragmas themselves.

Two stores share one schema: the per-profile store (``calendar.db``) and the
shared global store (``calendar-global.db``). The repository is the only
supported write path.
"""

from __future__ import annotations

from .connection import connect
from .errors import (
    CalendarNotFoundError,
    DatabaseError,
    EventNotFoundError,
    NotFoundError,
    RepositoryError,
    RevisionConflict,
    SchemaVersionError,
    SourceNotFoundError,
    StoreScopeError,
)
from .migrations import SCHEMA_VERSION, migrate
from .repository import Origin, Repository
from .store import (
    GLOBAL_DB_FILENAME,
    PROFILE_DB_FILENAME,
    open_global_store,
    open_profile_store,
    profile_name,
)

__all__ = [
    "GLOBAL_DB_FILENAME",
    "PROFILE_DB_FILENAME",
    "SCHEMA_VERSION",
    "CalendarNotFoundError",
    "DatabaseError",
    "EventNotFoundError",
    "NotFoundError",
    "Origin",
    "Repository",
    "RepositoryError",
    "RevisionConflict",
    "SchemaVersionError",
    "SourceNotFoundError",
    "StoreScopeError",
    "connect",
    "migrate",
    "open_global_store",
    "open_profile_store",
    "profile_name",
]
