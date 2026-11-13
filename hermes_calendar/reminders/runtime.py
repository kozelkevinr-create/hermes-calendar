"""Process-level tick wiring shared by the CLI and the in-process ticker.

Every tick opens its own short-lived SQLite connections. That is what makes the
ticker safe inside an asyncio thread: no connection crosses a thread boundary and
no profile path is cached. The global store is best-effort: if it cannot be
opened, profile-local reminders still work and the result records the degraded
state.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable

from ..db.reminders import ReminderStore
from ..db.store import open_global_store, open_profile_store
from ..profile_identity import current_profile_id
from .adapters import Notifier
from .engine import ReminderEngine
from .models import NotificationSettings, TickResult


def run_tick(
    *,
    now: int | None = None,
    settings: NotificationSettings | None = None,
    chat_notifier: Notifier | None = None,
    clock: Callable[[], float] = time.time,
    rng: Callable[[], float] = random.random,
) -> TickResult:
    """Open the active profile store, run one tick, and close everything."""
    profile_id = current_profile_id()
    profile_connection = open_profile_store()
    global_connection: sqlite3.Connection | None = None
    try:
        try:
            global_connection = open_global_store()
        except (sqlite3.Error, OSError):
            global_connection = None
        engine = ReminderEngine(
            profile_connection=profile_connection,
            profile_id=profile_id,
            global_connection=global_connection,
            settings=settings,
            chat_notifier=chat_notifier,
            clock=clock,
            rng=rng,
        )
        return engine.tick(now)
    finally:
        if global_connection is not None:
            global_connection.close()
        profile_connection.close()


def ticker_enabled(profile_connection: sqlite3.Connection) -> tuple[bool, int]:
    """Return ``(enabled, interval_seconds)`` from the profile's preferences."""
    from .config import load_settings

    settings = load_settings(ReminderStore(profile_connection))
    return settings.in_process_ticker_enabled, settings.ticker_interval_seconds
