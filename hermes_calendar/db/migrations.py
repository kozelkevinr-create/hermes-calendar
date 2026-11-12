"""Schema migration runner.

``PRAGMA user_version`` is the fast startup check; ``schema_meta`` carries the
human-readable store identity. A migration runs in one ``BEGIN IMMEDIATE``
transaction with change capture suppressed, and re-checks the version after
taking the write lock so several profile processes can race to create the shared
global store safely (``core.md`` §5.9).

Migrations are per store. Opening a store whose version is newer than this build
is refused rather than silently downgraded.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Literal

from hermes_calendar.db.errors import SchemaVersionError, StoreScopeError
from hermes_calendar.db.schema import V1_STATEMENTS

StoreScope = Literal["profile", "global"]

SCHEMA_VERSION = 1
LOCAL_SOURCE_ID = "src_local"
GLOBAL_CALENDAR_ID = "cal_global"
GLOBAL_STORE_ID = "global"


def migrate(
    connection: sqlite3.Connection,
    *,
    store_scope: StoreScope,
    store_id: str,
    owner_profile_id: str | None,
) -> int:
    """Create or upgrade the store schema and return the resulting version.

    Args:
        connection: An autocommit connection from :func:`open_db`.
        store_scope: ``"profile"`` or ``"global"``; must match an existing
            store's ``schema_meta.store_scope``.
        store_id: Cross-store identity, e.g. ``profile:coding`` or ``global``.
        owner_profile_id: Profile name for a profile store. The global store
            records an empty string, per the design.

    Raises:
        ValueError: if the requested scope is unknown or a profile store has no
            owner.
        SchemaVersionError: if the store was written by a newer build.
        StoreScopeError: if an existing store belongs to the other scope.
    """
    owner = _normalise_owner(store_scope, owner_profile_id)
    version = _read_user_version(connection)
    _reject_newer(version)

    if version == SCHEMA_VERSION:
        _verify_scope(connection, store_scope)
        return version

    connection.execute("BEGIN IMMEDIATE")
    try:
        # Another profile may have completed the migration while we waited for
        # the write lock, so the pre-lock check is not authoritative.
        version = _read_user_version(connection)
        _reject_newer(version)
        if version == 0:
            _apply_v1(
                connection,
                store_scope=store_scope,
                store_id=store_id,
                owner_profile_id=owner,
            )
        else:
            _verify_scope(connection, store_scope)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise

    return _read_user_version(connection)


def _normalise_owner(store_scope: StoreScope, owner_profile_id: str | None) -> str:
    if store_scope == "global":
        return ""
    if store_scope != "profile":
        raise ValueError(f"unknown store scope: {store_scope!r}")
    if not owner_profile_id:
        raise ValueError("a profile store requires owner_profile_id")
    return owner_profile_id


def _read_user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _reject_newer(version: int) -> None:
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"store schema version {version} is newer than supported {SCHEMA_VERSION}"
        )


def _verify_scope(connection: sqlite3.Connection, expected: StoreScope) -> None:
    row = connection.execute("SELECT value FROM schema_meta WHERE key = 'store_scope'").fetchone()
    actual = None if row is None else str(row["value"])
    if actual != expected:
        raise StoreScopeError(f"store scope {actual!r} does not match requested scope {expected!r}")


def _apply_v1(
    connection: sqlite3.Connection,
    *,
    store_scope: StoreScope,
    store_id: str,
    owner_profile_id: str,
) -> None:
    for statement in V1_STATEMENTS:
        connection.execute(statement)

    now = int(time.time())
    connection.execute("UPDATE change_context SET suppress = 1 WHERE id = 1")
    connection.executemany(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        (
            ("schema_version", str(SCHEMA_VERSION)),
            ("created_by", "hermes-calendar"),
            ("min_available_seq", "1"),
            ("store_scope", store_scope),
            ("store_id", store_id),
            ("owner_profile_id", owner_profile_id),
        ),
    )
    connection.execute(
        """
        INSERT INTO sources
          (id, scope, owner_profile_id, kind, display_name,
           config_json, capabilities_json, created_utc, updated_utc)
        VALUES (?, ?, ?, 'local', 'Local', '{}', '{}', ?, ?)
        """,
        (
            LOCAL_SOURCE_ID,
            store_scope,
            None if store_scope == "global" else owner_profile_id,
            now,
            now,
        ),
    )
    if store_scope == "global":
        connection.execute(
            """
            INSERT INTO calendars
              (id, source_id, scope, owner_profile_id, name, default_tz,
               read_only, created_utc, updated_utc)
            VALUES (?, ?, 'global', NULL, 'Global', 'UTC', 0, ?, ?)
            """,
            (GLOBAL_CALENDAR_ID, LOCAL_SOURCE_ID, now, now),
        )
    connection.execute("UPDATE change_context SET suppress = 0 WHERE id = 1")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
