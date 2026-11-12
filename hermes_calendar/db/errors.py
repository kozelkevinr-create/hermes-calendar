"""Errors raised by the database layer.

Repository callers distinguish missing rows (re-read or return 404) from a lost
compare-and-swap (409 conflict, re-read and retry) and from schema problems
(refuse to open rather than corrupt data).
"""

from __future__ import annotations


class DatabaseError(Exception):
    """Base class for database-layer errors."""


class SchemaVersionError(DatabaseError):
    """The store schema is newer than this build supports."""


class StoreScopeError(DatabaseError):
    """The store's ``schema_meta.store_scope`` disagrees with the requested one."""


class RepositoryError(DatabaseError):
    """Base class for repository operation failures."""


class NotFoundError(RepositoryError):
    """The requested row does not exist in this store."""


class SourceNotFoundError(NotFoundError):
    """No usable source row exists."""


class CalendarNotFoundError(NotFoundError):
    """No usable calendar row exists."""


class EventNotFoundError(NotFoundError):
    """No event row exists."""


class RevisionConflict(RepositoryError):
    """A compare-and-swap on ``events.rev`` lost the race."""

    def __init__(self, event_id: str, expected_rev: int) -> None:
        super().__init__(
            f"event {event_id} changed: expected rev {expected_rev}, retry after re-reading"
        )
        self.event_id = event_id
        self.expected_rev = expected_rev
