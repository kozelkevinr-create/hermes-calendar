"""Hermes registration for the calendar plugin.

Hermes calls :func:`register` once per load. Every tool, hook, command, and
provider the plugin exposes must be registered from here (or from a function
this calls) so the plugin manager can tear it down on unload/reload.

The scaffold intentionally registers nothing yet. Subsystems add their surfaces
here and mirror them in ``plugin.yaml`` (``provides_tools`` /
``provides_hooks``), because ``hermes plugins validate`` fails on drift.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Register the calendar plugin's Hermes surfaces.

    Args:
        ctx: Hermes ``PluginContext``. Typed as ``Any`` because Hermes is a
            runtime dependency that is not installed in the test environment.

    Registrations land with their subsystems:
      * calendar CRUD tools and ``hermes-calendar`` CLI commands,
      * reminder hooks / cron safety net,
      * the web UI service and sync adapters.
    """
    _ = ctx
