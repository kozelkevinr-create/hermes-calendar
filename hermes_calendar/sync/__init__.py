"""Calendar sync engine and remote adapters.

Owned by the sync design (``docs/design/sync.md``). The core model and change
log live in :mod:`hermes_calendar.db`; this package translates between them and
remote backends without adding parallel event tables.
"""
