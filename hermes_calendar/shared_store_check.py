"""Generic shared-store sentinel check.

This module is copied verbatim into other plugin repositories, so it must stay
free of product-specific names, keep no module-level state, and depend only on
the standard library plus the sibling ``plugin_storage`` helper.

What it answers: is this plugin's global data directory usable, and has another
profile actually used it? A writable path proves nothing about sharing, so
:func:`verify_shared` only reports ``shared=True`` after it observes another
profile's fresh sentinel row in ``<_shared_check.db>``.

The check never raises: a read-only filesystem, permission error, locked or
corrupt database, and a filesystem that refuses WAL all come back as
``ok=False`` with a human-readable ``detail``. Filesystems that silently keep a
rollback journal are reported in ``detail`` as well, because SQLite locking is
unreliable there.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .plugin_storage import global_dir, open_db

__all__ = ["SharedStoreStatus", "verify_shared"]

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_DB_FILENAME = "_shared_check.db"
_POLL_INTERVAL_S = 0.02
_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS shared_sentinel ("
    "profile_id TEXT PRIMARY KEY, "
    "last_seen REAL NOT NULL, "
    "pid INTEGER NOT NULL"
    ")"
)
_UPSERT = (
    "INSERT INTO shared_sentinel (profile_id, last_seen, pid) VALUES (?, ?, ?) "
    "ON CONFLICT(profile_id) DO UPDATE SET "
    "last_seen = excluded.last_seen, pid = excluded.pid"
)
_SELECT_OTHERS = "SELECT profile_id, last_seen FROM shared_sentinel WHERE profile_id <> ?"


@dataclass(frozen=True)
class SharedStoreStatus:
    """Outcome of :func:`verify_shared`.

    Attributes:
        ok: The sentinel database could be opened, written, and read.
        shared: ``True`` only when at least one other profile's fresh sentinel
            was observed. ``False`` when other profiles have registered in the
            past but every one of them is stale. ``None`` when no other profile
            has ever registered, so sharing is simply unknown.
        peers_seen: Fresh other profile ids, sorted.
        writable: This profile's sentinel row was committed successfully.
        detail: Human-readable explanation, including any filesystem warning.
    """

    ok: bool
    shared: bool | None
    peers_seen: tuple[str, ...]
    writable: bool
    detail: str


@dataclass(frozen=True)
class _Observation:
    """One read of the sentinel table from this profile's point of view."""

    fresh: tuple[str, ...]
    known_peers: int

    @property
    def shared(self) -> bool | None:
        """Tri-state sharing answer derived from the observed rows."""
        if self.fresh:
            return True
        return False if self.known_peers else None


def verify_shared(
    plugin_name: str,
    profile_id: str,
    *,
    wait_s: float = 0.0,
    stale_after_s: float = 300.0,
    clock: Callable[[], float] = time.time,
) -> SharedStoreStatus:
    """Probe a plugin's shared data directory and register this profile.

    The probe creates ``<_shared_check.db>`` in ``global_dir(plugin_name)``,
    upserts this profile's sentinel row inside ``BEGIN IMMEDIATE``, and reads
    the other rows. Rows older than ``stale_after_s`` seconds are ignored, so a
    crashed profile cannot keep the store looking shared. With ``wait_s > 0``
    the probe keeps reading until it sees a fresh peer or the budget runs out.

    Args:
        plugin_name: Plugin data directory name; letters, digits, ``-``, ``_``.
        profile_id: This profile's id; validated like a plugin name.
        wait_s: Seconds to keep polling for another profile. Zero reads once.
        stale_after_s: Age in seconds after which a peer sentinel is ignored.
        clock: Time source for sentinel timestamps, injectable for tests.

    Returns:
        A :class:`SharedStoreStatus`. Every failure, including an invalid
        argument, is reported with ``ok=False`` instead of raising.
    """
    invalid = _invalid_argument(plugin_name, profile_id)
    if invalid is not None:
        return SharedStoreStatus(False, None, (), False, invalid)

    try:
        wait = max(0.0, float(wait_s))
        stale_after = max(0.0, float(stale_after_s))
    except (TypeError, ValueError) as exc:
        return SharedStoreStatus(
            False,
            None,
            (),
            False,
            f"wait_s and stale_after_s must be numbers: {exc}",
        )

    try:
        directory = global_dir(plugin_name)
    except Exception as exc:  # noqa: BLE001 - the probe contract is never to raise
        return SharedStoreStatus(
            False,
            None,
            (),
            False,
            f"shared data directory is unavailable: {exc}",
        )

    connection: sqlite3.Connection | None = None
    writable = False
    try:
        connection = open_db(directory / _DB_FILENAME)
        _register(connection, profile_id, clock())
        writable = True
        observation = _observe(connection, profile_id, clock(), stale_after)
        if observation.shared is not True and wait > 0.0:
            observation = _wait_for_peer(connection, profile_id, wait, stale_after, clock)
        warning = _journal_warning(connection)
        return SharedStoreStatus(
            True,
            observation.shared,
            observation.fresh,
            writable,
            _summary(observation, stale_after, warning),
        )
    except Exception as exc:  # noqa: BLE001 - the probe contract is never to raise
        return SharedStoreStatus(False, None, (), writable, _failure_detail(exc))
    finally:
        if connection is not None:
            # A failing close must not hide the result the probe already has.
            with contextlib.suppress(Exception):
                connection.close()


def _invalid_argument(plugin_name: str, profile_id: str) -> str | None:
    """Return a detail string when either identifier is unsafe, else ``None``."""
    if not isinstance(plugin_name, str) or _ID_RE.fullmatch(plugin_name) is None:
        return (
            "invalid plugin name: expected letters, digits, '-' and '_' "
            f"(max 64 characters, starting with a letter or digit); got {plugin_name!r}"
        )
    if not isinstance(profile_id, str) or _ID_RE.fullmatch(profile_id) is None:
        return (
            "invalid profile id: expected letters, digits, '-' and '_' "
            f"(max 64 characters, starting with a letter or digit); got {profile_id!r}"
        )
    return None


def _register(connection: sqlite3.Connection, profile_id: str, now: float) -> None:
    """Create the sentinel table and refresh this profile's row atomically.

    Closing the connection rolls back an interrupted transaction, so the caller
    does not need a rollback of its own.
    """
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(_CREATE_TABLE)
    connection.execute(_UPSERT, (profile_id, now, os.getpid()))
    connection.execute("COMMIT")


def _observe(
    connection: sqlite3.Connection,
    profile_id: str,
    now: float,
    stale_after_s: float,
) -> _Observation:
    """Read the other sentinel rows, keeping only fresh profile ids."""
    cutoff = now - stale_after_s
    rows = connection.execute(_SELECT_OTHERS, (profile_id,)).fetchall()
    fresh = tuple(sorted(str(row[0]) for row in rows if float(row[1]) >= cutoff))
    return _Observation(fresh=fresh, known_peers=len(rows))


def _wait_for_peer(
    connection: sqlite3.Connection,
    profile_id: str,
    wait_s: float,
    stale_after_s: float,
    clock: Callable[[], float],
) -> _Observation:
    """Poll the sentinel table until a fresh peer appears or the budget expires."""
    deadline = time.monotonic() + wait_s
    observation = _observe(connection, profile_id, clock(), stale_after_s)
    while observation.shared is not True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        time.sleep(min(_POLL_INTERVAL_S, remaining))
        observation = _observe(connection, profile_id, clock(), stale_after_s)
    return observation


def _journal_warning(connection: sqlite3.Connection) -> str:
    """Describe a non-WAL journal mode, or return an empty string for WAL."""
    row = connection.execute("PRAGMA journal_mode").fetchone()
    mode = "" if row is None else str(row[0]).lower()
    if mode == "wal":
        return ""
    label = mode or "unknown"
    return (
        f"database journal mode is {label!r}, not 'wal'; "
        "locking may be unreliable on this filesystem"
    )


def _summary(observation: _Observation, stale_after_s: float, warning: str) -> str:
    """Explain the sharing answer, appending any filesystem warning."""
    if observation.shared is True:
        detail = "shared store is reachable and active; fresh other profiles: " + ", ".join(
            observation.fresh
        )
    elif observation.shared is False:
        detail = (
            "shared store is reachable, but every other profile sentinel is stale "
            f"(older than {stale_after_s:g}s)"
        )
    else:
        detail = (
            "shared store is reachable, but only this profile has registered; "
            "shared status is unknown"
        )
    return f"{detail}; {warning}" if warning else detail


def _failure_detail(error: Exception) -> str:
    """Turn an exception into a short, actionable description."""
    text = str(error) or type(error).__name__
    lowered = text.lower()
    if "locked" in lowered or "busy" in lowered:
        return f"shared store database is locked; try again: {text}"
    if (
        isinstance(error, PermissionError)
        or "readonly" in lowered
        or "read-only" in lowered
        or "unable to open database" in lowered
        or "permission" in lowered
    ):
        return f"shared store is not writable (read-only or permission denied): {text}"
    if isinstance(error, sqlite3.DatabaseError):
        return f"shared store database is unreadable or corrupt: {text}"
    if isinstance(error, OSError):
        return f"shared store is not accessible: {text}"
    return f"shared store check failed: {type(error).__name__}: {text}"
