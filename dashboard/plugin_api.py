"""Dashboard backend entry point for the calendar plugin.

Hermes imports this file by path (not as a package) and mounts its ``router`` at
``/api/plugins/hermes-calendar/``. It is not imported by ``hermes_calendar``'s
``register()`` path, so FastAPI remains an optional dashboard-side dependency.

The implementation lives in :mod:`hermes_calendar.ui.dashboard_api` so it can
use the package's relative imports. This file only puts the plugin root on
``sys.path`` when Hermes has not already made the package importable, then
re-exports the router.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if _PLUGIN_ROOT not in sys.path:
    # Hermes imports this module standalone; append (never prepend) so the
    # standard library and the dashboard's own packages keep priority.
    sys.path.append(_PLUGIN_ROOT)

from hermes_calendar.ui.dashboard_api import router  # noqa: E402 - path bootstrap above

__all__ = ["router"]
