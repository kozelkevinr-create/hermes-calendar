"""Per-plugin persistent storage: ``<hermes home>/plugin-data/<name>/``.

Generic by design: this module is copied verbatim into other plugin repositories,
so it must not mention any particular product, keep no module-level path cache,
and depend only on the standard library plus the optional ``hermes_constants``
module that Hermes provides at runtime.

Plugins must not park state in ``<hermes home>/plugins/<name>/``: that directory
is the install tree, replaced by ``update`` and deleted by ``remove``.

Resolution rules (both resolved on *every* call, never cached):

* :func:`profile_dir` follows the active profile through
  ``hermes_constants.get_hermes_home()``.
* :func:`global_dir` follows the root that owns all profiles through
  ``hermes_constants.get_default_hermes_root()``.

Outside a Hermes host (tests, tooling) the module falls back to ``HERMES_HOME``
and to the platform default home. Secrets are deliberately not part of this
convention; keep credentials in the host's secret store.
"""

from __future__ import annotations

import importlib
import os
import re
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

__all__ = ["global_dir", "open_db", "profile_dir"]

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_BUSY_TIMEOUT_MS = 5_000


class _HermesConstants(Protocol):
    """The small slice of ``hermes_constants`` this module relies on."""

    def get_hermes_home(self) -> Path: ...

    def get_default_hermes_root(self) -> Path: ...


def _hermes_constants() -> _HermesConstants | None:
    """Return ``hermes_constants`` when importable, else ``None``.

    Imported lazily so this module also works in tests and off-host tooling.
    The module is looked up on every call, but Python caches it in
    ``sys.modules``; no path is cached here.
    """
    try:
        module: ModuleType = importlib.import_module("hermes_constants")
    except ImportError:
        return None
    return cast(_HermesConstants, module)


def _validate_name(plugin_name: str) -> str:
    """Validate a plugin name as a single path component.

    Letters, digits, ``-`` and ``_`` only, starting with a letter or digit.
    Rejects path separators, ``..``, dots, spaces, and empty names so a caller
    cannot escape ``plugin-data/``.
    """
    if (
        not isinstance(plugin_name, str)
        or not _NAME_RE.fullmatch(plugin_name)
        or ".." in plugin_name
    ):
        raise ValueError(f"invalid plugin name for storage: {plugin_name!r}")
    return plugin_name


def _fallback_profile_home() -> Path:
    """Profile home when ``hermes_constants`` is unavailable."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return Path.home() / ".hermes"
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def _fallback_global_root() -> Path:
    """Root owning all profiles when ``hermes_constants`` is unavailable.

    A profile home normally lives at ``<root>/profiles/<name>``; strip the
    ``profiles/<name>`` segment. Any other ``HERMES_HOME`` is already a root.
    """
    home = _fallback_profile_home()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_dir(plugin_name: str) -> Path:
    """Return (and create) ``<profile home>/plugin-data/<plugin_name>``.

    The home is resolved on every call so the result follows the active
    profile; do not cache the returned path across profile switches.
    """
    name = _validate_name(plugin_name)
    constants = _hermes_constants()
    home = constants.get_hermes_home() if constants is not None else _fallback_profile_home()
    return _ensure_directory(home / "plugin-data" / name)


def global_dir(plugin_name: str) -> Path:
    """Return (and create) ``<hermes root>/plugin-data/<plugin_name>``.

    The root is resolved on every call so it follows a moving or overridden
    Hermes home.
    """
    name = _validate_name(plugin_name)
    constants = _hermes_constants()
    root = constants.get_default_hermes_root() if constants is not None else _fallback_global_root()
    return _ensure_directory(root / "plugin-data" / name)


def open_db(path: Path) -> sqlite3.Connection:
    """Open ``path`` as a WAL SQLite database with the shared pragma set.

    Applied pragmas: ``busy_timeout=5000``, ``foreign_keys=ON``, and WAL for
    file-backed databases (skipped for ``:memory:``). The connection uses
    ``row_factory = sqlite3.Row``, ``isolation_level = None`` (autocommit, so
    callers open explicit transactions), and ``check_same_thread=False`` so a
    background worker and a tool call can share it.

    Parent directories are created. The caller owns the connection and its
    transactions.
    """
    database = Path(path).expanduser()
    target = str(database)
    if target != ":memory:":
        database.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        target,
        timeout=_BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if target != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
    except BaseException:
        connection.close()
        raise
    return connection
