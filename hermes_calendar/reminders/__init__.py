"""Reminder scheduling, claim/lease, routing, and delivery adapters.

Owned by the app design (``docs/design/app-notifications.md``). The in-process
ticker and the ``hermes cron`` safety net both enter through
:func:`hermes_calendar.reminders.runtime.run_tick`.

Importing this package has no side effects: no thread, no database, and no
subprocess is created until a tick runs or the host explicitly enables the
optional accelerator.
"""

from __future__ import annotations

from .engine import ProfileMismatchError, ReminderEngine
from .models import Notification, NotificationSettings, TickResult
from .runtime import run_tick

__all__ = [
    "Notification",
    "NotificationSettings",
    "ProfileMismatchError",
    "ReminderEngine",
    "TickResult",
    "run_tick",
]
