"""Hermes plugin entry point for hermes-calendar.

Hermes loads this module from the plugin directory and calls ``register(ctx)``
exactly once at load time. Keep this file thin: it exists so Hermes finds the
entry point beside ``plugin.yaml``; the implementation lives in the
``hermes_calendar`` package.
"""

from __future__ import annotations

from hermes_calendar.plugin import register

__all__ = ["register"]
