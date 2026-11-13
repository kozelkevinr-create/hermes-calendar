"""Schema migration runner.

``PRAGMA user_version`` is the fast startup check; ``schema_meta`` carries the
human-readable store identity. A migration runs in one ``BEGIN IMMEDIATE``
transaction with change capture suppressed, and re-checks the version after
taking the write lock so several profile processes can race to create the shared
global store safely (``core.md`` §5.9).

Migrations are per store. Opening a store whose version is newer than this build
is refused rather than silently downgraded. Versions are additive: a v1 store
upgrades in place by applying v2 and v3, and a fresh store applies v1, v2, and v3
inside the same write transaction.

V3 seeds one default ``Personal`` calendar into a profile store that has never
had any calendar. The insert is attributed to ``origin='system'`` so it is never
offered to a remote provider, yet it still produces the ordinary calendar
change-log row (``core.md`` §7.3) for local refresh handling.

V4 repairs the profile id written by the old directory-basename derivation. A
default-profile store may hold ``.hermes`` in its ``schema_meta`` identity and
in every owner/actor column; v4 rewrites exactly that value to ``default`` after
taking a private backup. It never creates a change-log row: the rewrite runs
with change capture suppressed and leaves ``pending_push`` untouched. Stores
with no legacy rows (including fresh stores) are upgraded without a backup.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..models import new_uuid7
from .errors import MigrationError, SchemaVersionError, StoreScopeError
from .schema import V1_STATEMENTS, V2_GLOBAL_STATEMENTS, V2_PROFILE_STATEMENTS

StoreScope = Literal["profile", "global"]

SCHEMA_VERSION = 4
V1_SCHEMA_VERSION = 1
V2_SCHEMA_VERSION = 2
V3_SCHEMA_VERSION = 3
V4_SCHEMA_VERSION = 4
LOCAL_SOURCE_ID = "src_local"
GLOBAL_CALENDAR_ID = "cal_global"
GLOBAL_STORE_ID = "global"
DEFAULT_PROFILE_CALENDAR_NAME = "Personal"
LEGACY_PROFILE_ID = ".hermes"
DEFAULT_PROFILE_ID = "default"
_IDENTITY_COLUMN_RE = re.compile(r"profile|actor|creator", re.IGNORECASE)


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
        if version < V2_SCHEMA_VERSION:
            _apply_v2(connection, store_scope=store_scope)
        if version < V3_SCHEMA_VERSION:
            _apply_v3(connection, store_scope=store_scope)
        if version < V4_SCHEMA_VERSION:
            _apply_v4(connection, store_scope=store_scope)
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
            ("schema_version", str(V1_SCHEMA_VERSION)),
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
    connection.execute(f"PRAGMA user_version = {V1_SCHEMA_VERSION}")


def _apply_v2(connection: sqlite3.Connection, *, store_scope: StoreScope) -> None:
    """Add notification tables (profile) or coordination tables (global).

    The two stores deliberately get different v2 tables: ``core.md`` §3.7 says
    the global store holds no notification content state, while the app design
    needs one shared lease and a content-free dedup ledger to prevent duplicate
    global-calendar delivery across profiles (``app-notifications.md`` §6.2).
    """
    statements = V2_PROFILE_STATEMENTS if store_scope == "profile" else V2_GLOBAL_STATEMENTS
    for statement in statements:
        connection.execute(statement)

    if store_scope == "profile":
        now = int(time.time())
        # The in-app channel is always available. The chat channel starts
        # disabled with no target: the target is explicit setup configuration,
        # never inferred from ``hermes send --list`` or the environment.
        connection.executemany(
            """
            INSERT INTO notification_channels
              (id, kind, enabled, config_json, secret_ref, created_utc)
            VALUES (?, ?, ?, '{}', NULL, ?)
            """,
            (
                ("in-app", "in_app", 1, now),
                ("phone", "hermes_send", 0, now),
            ),
        )

    connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(V2_SCHEMA_VERSION),),
    )
    connection.execute(f"PRAGMA user_version = {V2_SCHEMA_VERSION}")


def _apply_v3(connection: sqlite3.Connection, *, store_scope: StoreScope) -> None:
    """Seed one default calendar into a profile store that has no calendars.

    The seed is deliberately data-only and runs exactly once per store: it
    counts every calendar row, including soft-deleted ones, so a user who
    removed their calendars is never given one back by a later open. The global
    store is untouched and keeps exactly its single ``cal_global`` row.
    """
    if store_scope == "profile":
        _seed_default_profile_calendar(connection)

    connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(V3_SCHEMA_VERSION),),
    )
    connection.execute(f"PRAGMA user_version = {V3_SCHEMA_VERSION}")


def _seed_default_profile_calendar(connection: sqlite3.Connection) -> None:
    """Insert the default calendar through the normal change-log path."""
    if connection.execute("SELECT 1 FROM calendars LIMIT 1").fetchone() is not None:
        return

    owner_row = connection.execute(
        "SELECT owner_profile_id FROM sources WHERE id = ?", (LOCAL_SOURCE_ID,)
    ).fetchone()
    if owner_row is None or owner_row["owner_profile_id"] is None:
        # A v1/v2 profile store always seeds ``src_local`` with an owner. If an
        # externally modified store lacks it, leave the store without a
        # calendar rather than inventing a foreign key target.
        return

    now = int(time.time())
    connection.execute(
        """
        UPDATE change_context
           SET origin = 'system', actor = NULL, batch_id = 'migration:v3',
               suppress = 0
         WHERE id = 1
        """
    )
    # ``default_tz`` is omitted on purpose: the column default keeps the seed
    # zone-neutral so the normal timezone resolution order applies at read time.
    connection.execute(
        """
        INSERT INTO calendars
          (id, source_id, scope, owner_profile_id, name, description, color,
           read_only, created_utc, updated_utc)
        VALUES (?, ?, 'profile', ?, ?, NULL, NULL, 0, ?, ?)
        """,
        (
            f"cal_{new_uuid7()}",
            LOCAL_SOURCE_ID,
            str(owner_row["owner_profile_id"]),
            DEFAULT_PROFILE_CALENDAR_NAME,
            now,
            now,
        ),
    )
    connection.execute(
        """
        UPDATE change_context
           SET origin = 'local', actor = NULL, batch_id = NULL
         WHERE id = 1
        """
    )


def _apply_v4(connection: sqlite3.Connection, *, store_scope: StoreScope) -> None:
    """Repair the legacy ``.hermes`` profile identity in a profile store.

    The global store has no profile identity to repair (its ``owner_profile_id``
    values are ``NULL`` and its co-ordination rows are written by the current
    code path, which now supplies a valid id), so only the version is bumped.
    """
    if store_scope == "profile":
        _rewrite_legacy_profile_identity(connection)

    connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(V4_SCHEMA_VERSION),),
    )
    connection.execute(f"PRAGMA user_version = {V4_SCHEMA_VERSION}")


def _rewrite_legacy_profile_identity(connection: sqlite3.Connection) -> None:
    """Rewrite every exact ``.hermes`` identity value to ``default``.

    The backup is taken only when at least one row actually needs changing, and
    it is taken before the first write. The rewrite runs inside the caller's
    ``BEGIN IMMEDIATE`` transaction with ``change_context.suppress = 1`` so no
    update trigger appends a change-log row; the preceding context row is then
    restored (with a legacy actor promoted to ``default`` rather than put back).
    """
    if _legacy_identity_rows(connection) == 0:
        return

    _backup_database(connection)

    context = connection.execute(
        "SELECT origin, actor, batch_id, suppress FROM change_context WHERE id = 1"
    ).fetchone()
    columns = _identity_columns(connection)

    connection.execute("UPDATE change_context SET suppress = 1 WHERE id = 1")
    try:
        for table, column in columns:
            connection.execute(
                f"UPDATE {_quote(table)} SET {_quote(column)} = ? WHERE {_quote(column)} = ?",
                (DEFAULT_PROFILE_ID, LEGACY_PROFILE_ID),
            )
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE value = ?",
            (DEFAULT_PROFILE_ID, LEGACY_PROFILE_ID),
        )
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'store_id' AND value = ?",
            (f"profile:{DEFAULT_PROFILE_ID}", f"profile:{LEGACY_PROFILE_ID}"),
        )
    finally:
        if context is None:  # pragma: no cover - change_context always has row 1
            connection.execute("UPDATE change_context SET suppress = 0 WHERE id = 1")
        else:
            previous_actor = context["actor"]
            restored_actor = (
                DEFAULT_PROFILE_ID if previous_actor == LEGACY_PROFILE_ID else previous_actor
            )
            connection.execute(
                """
                UPDATE change_context
                   SET origin = ?, actor = ?, batch_id = ?, suppress = ?
                 WHERE id = 1
                """,
                (
                    context["origin"],
                    restored_actor,
                    context["batch_id"],
                    context["suppress"],
                ),
            )


def _identity_columns(connection: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """Find every column whose name marks a profile id, actor, or creator.

    The schema is searched rather than hard-coded so a table added later with an
    owner/actor column is repaired by the same migration. ``schema_meta`` is
    handled separately because its identity lives in key/value rows.
    """
    tables = connection.execute(
        """
        SELECT name FROM sqlite_master
         WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
         ORDER BY name
        """
    ).fetchall()
    columns: list[tuple[str, str]] = []
    for table_row in tables:
        table = str(table_row["name"])
        for column_row in connection.execute(f"PRAGMA table_info({_quote(table)})"):
            column = str(column_row["name"])
            if _IDENTITY_COLUMN_RE.search(column):
                columns.append((table, column))
    return tuple(columns)


def _legacy_identity_rows(connection: sqlite3.Connection) -> int:
    """Count rows that :func:`_rewrite_legacy_profile_identity` would change."""
    total = 0
    for table, column in _identity_columns(connection):
        row = connection.execute(
            f"SELECT count(*) FROM {_quote(table)} WHERE {_quote(column)} = ?",
            (LEGACY_PROFILE_ID,),
        ).fetchone()
        total += int(row[0])
    total += int(
        connection.execute(
            "SELECT count(*) FROM schema_meta WHERE value = ?", (LEGACY_PROFILE_ID,)
        ).fetchone()[0]
    )
    total += int(
        connection.execute(
            "SELECT count(*) FROM schema_meta WHERE key = 'store_id' AND value = ?",
            (f"profile:{LEGACY_PROFILE_ID}",),
        ).fetchone()[0]
    )
    return total


def _backup_database(connection: sqlite3.Connection) -> Path:
    """Write a consistent private backup next to the database file.

    A second connection is used because SQLite's backup API cannot read from the
    connection that holds the migration's write transaction. No rewrite has
    happened yet, so that connection sees the pre-migration state.

    The destination is created with ``O_EXCL`` and mode ``0600``; an existing
    backup is never overwritten. On failure the partial file is removed and
    :class:`MigrationError` aborts the migration before the transaction commits.
    """
    database = _database_path(connection)
    destination = _backup_path(database)
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(destination, 0o600)
        source = sqlite3.connect(str(database), timeout=5)
        try:
            source.execute("PRAGMA busy_timeout = 5000")
            target = sqlite3.connect(str(destination))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        with contextlib.suppress(OSError):
            destination.unlink()
        raise MigrationError(
            f"cannot create the pre-v4 backup {destination.name} for {database}: {exc}"
        ) from exc
    return destination


def _database_path(connection: sqlite3.Connection) -> Path:
    """Return the file backing the ``main`` database."""
    for row in connection.execute("PRAGMA database_list"):
        if str(row["name"]) == "main":
            raw = str(row["file"])
            if raw:
                return Path(raw)
    raise MigrationError("cannot create a pre-v4 backup for an in-memory store")


def _backup_path(database: Path) -> Path:
    """Return a non-existent ``<db>.pre-v4-<UTC timestamp>.bak`` path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = database.with_name(f"{database.name}.pre-v4-{stamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = database.with_name(f"{database.name}.pre-v4-{stamp}-{suffix}.bak")
        suffix += 1
    return candidate


def _quote(identifier: str) -> str:
    """Quote a schema identifier for interpolation into DDL/DML."""
    return '"' + identifier.replace('"', '""') + '"'
