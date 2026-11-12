"""Hermes registration for the calendar plugin.

Hermes calls :func:`register` once per load. Every tool, hook, command, and
provider the plugin exposes is registered from here (or from a function this
calls) so the plugin manager can tear it down on unload/reload.

Declared surfaces in ``plugin.yaml`` (``provides_tools`` / ``provides_hooks``)
must match what this module registers; ``hermes plugins validate`` fails on
drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_calendar import __version__
from hermes_calendar.plugin_storage import global_dir, profile_dir

PLUGIN_NAME = "hermes-calendar"
PROFILE_DB_FILENAME = "calendar.db"
GLOBAL_DB_FILENAME = "calendar-global.db"

CALENDAR_STATUS_SCHEMA: dict[str, Any] = {
    "name": "calendar_status",
    "description": (
        "Report the calendar plugin version, its profile and global data directories, "
        "and whether each calendar database file exists. Use this when the user asks "
        "where calendar data is stored or whether the database has been initialized."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def _database_status(directory: Path, filename: str) -> dict[str, str | bool]:
    """Describe one database file without creating it."""
    path = directory / filename
    return {"path": str(path), "exists": path.is_file()}


def calendar_status(_args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
    """Return plugin version, data locations, and database presence as JSON.

    Hermes calls tool handlers as ``handler(args, **kwargs)``; the argument
    object is empty today, so it is accepted and ignored.
    """
    profile = profile_dir(PLUGIN_NAME)
    global_ = global_dir(PLUGIN_NAME)
    payload: dict[str, Any] = {
        "plugin": PLUGIN_NAME,
        "version": __version__,
        "profile_data_dir": str(profile),
        "global_data_dir": str(global_),
        "databases": {
            "profile": _database_status(profile, PROFILE_DB_FILENAME),
            "global": _database_status(global_, GLOBAL_DB_FILENAME),
        },
    }
    return json.dumps(payload, sort_keys=True)


def register(ctx: Any) -> None:
    """Register the calendar plugin's Hermes surfaces.

    Args:
        ctx: Hermes ``PluginContext``. Typed as ``Any`` because Hermes is a
            runtime dependency that is not installed in the test environment.
    """
    ctx.register_tool(
        name="calendar_status",
        toolset="calendar",
        schema=CALENDAR_STATUS_SCHEMA,
        handler=calendar_status,
        description="Report plugin version and calendar data locations.",
    )
