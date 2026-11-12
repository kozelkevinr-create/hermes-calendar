"""RFC 5545 recurrence expansion.

Expansion happens in local wall-clock time using the event's IANA zone; see
``docs/design/core.md`` §5. Keep timezone-aware instant math in this package,
not in callers.
"""
