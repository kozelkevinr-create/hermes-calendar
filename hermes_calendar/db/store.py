"""Store locations and opening.

The profile store follows the active Hermes profile through
``plugin_storage.profile_dir``; the global store uses the shared root through
``plugin_storage.global_dir`` (``core.md`` §4.2). Both file names are fixed so the
status tool and operators see the same paths.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..plugin_storage import global_dir, profile_dir
from ..plugin_storage import open_db as open_db
from ..profile_identity import current_profile_id
from .migrations import (
    GLOBAL_STORE_ID,
    StoreScope,
    migrate,
)

PLUGIN_NAME = "hermes-calendar"
PROFILE_DB_FILENAME = "calendar.db"
GLOBAL_DB_FILENAME = "calendar-global.db"
_OPEN_RETRIES = 5
_OPEN_RETRY_DELAY_SECONDS = 0.05


def profile_name() -> str:
    """Return the active Hermes profile id (``default``, a name, or ``custom``).

    Kept as a stable name for existing callers; the value comes from
    :func:`hermes_calendar.profile_identity.current_profile_id`, never from the
    plugin-data directory basename.
    """
    return current_profile_id()


def open_profile_store() -> sqlite3.Connection:
    """Open and migrate the active profile's store.

    The caller owns the returned connection and must close it.
    """
    name = profile_name()
    path = profile_dir(PLUGIN_NAME) / PROFILE_DB_FILENAME
    return _open_migrated(
        path,
        store_scope="profile",
        store_id=f"profile:{name}",
        owner_profile_id=name,
    )


def open_global_store() -> sqlite3.Connection:
    """Open and migrate the shared global store.

    Several profile processes may call this concurrently. The migration runner
    serializes them with ``BEGIN IMMEDIATE`` and re-checks the schema version.
    """
    path = global_dir(PLUGIN_NAME) / GLOBAL_DB_FILENAME
    return _open_migrated(
        path,
        store_scope="global",
        store_id=GLOBAL_STORE_ID,
        owner_profile_id="",
    )


def _open_migrated(
    path: Path,
    *,
    store_scope: StoreScope,
    store_id: str,
    owner_profile_id: str | None,
) -> sqlite3.Connection:
    connection = _open_with_retry(path)
    try:
        migrate(
            connection,
            store_scope=store_scope,
            store_id=store_id,
            owner_profile_id=owner_profile_id,
        )
    except BaseException:
        connection.close()
        raise
    return connection


def _open_with_retry(path: Path) -> sqlite3.Connection:
    """Open a database, retrying lock/busy failures.

    Several profile processes may race to create the shared global store. SQLite
    can reject the initial ``journal_mode = WAL`` switch with ``database is
    locked`` before a connection exists to carry ``busy_timeout``; a short retry
    covers that window and matches the design's retry-with-backoff rule.
    """
    attempt = 0
    while True:
        try:
            return open_db(path)
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt >= _OPEN_RETRIES - 1:
                raise
            attempt += 1
            time.sleep(_OPEN_RETRY_DELAY_SECONDS)


def _is_busy_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message
