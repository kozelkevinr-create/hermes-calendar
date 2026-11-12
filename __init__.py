"""Hermes plugin entry point for hermes-calendar.

Hermes loads this module from the plugin directory as a package and calls
``register(ctx)`` exactly once at load time. Keep this file thin: it exists so
Hermes finds the entry point beside ``plugin.yaml``; the implementation lives in
the ``hermes_calendar`` package.

Imports are relative so the plugin works both as a Hermes directory plugin
(``<plugin dir>/__init__.py`` loaded as a package) and as the repository root
during development. Pytest collects the repository root as a package of its
own; in that case there is no parent package for a relative import, so fall
back to the top-level ``hermes_calendar`` package on ``sys.path``.
"""

from __future__ import annotations

if __package__:
    from .hermes_calendar.plugin import register
else:
    from hermes_calendar.plugin import register

__all__ = ["register"]
