"""Optional in-process reminder accelerator.

The design calls this an *optional accelerator*: ``hermes cron`` is the primary
trigger, and this loop is disabled by default. When explicitly enabled in
``notification_prefs`` (``in_process_ticker.enabled``), ``register(ctx)`` starts
it with ``ctx.spawn_task`` on the host's running event loop. Hermes tracks that
task as a ``background_task`` and cancels it on unload, so no bare thread is
created and no work runs at import time.

The tick body itself runs in a worker thread (``asyncio.to_thread``) with fresh
SQLite connections so it cannot block the loop or share a connection with a tool
call.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..db.store import open_profile_store
from .runtime import run_tick, ticker_enabled

DEFAULT_NAME = "calendar-reminders"


@dataclass
class InProcessTicker:
    """A cancellable async loop around one profile-scoped tick."""

    tick: Callable[[], object] = run_tick
    interval_seconds: float = 30
    name: str = DEFAULT_NAME
    _stopped: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    async def run(self) -> None:
        """Tick forever until cancelled or :meth:`stop` is called."""
        try:
            while not self._stopped.is_set():
                await asyncio.sleep(self.interval_seconds)
                if self._stopped.is_set():
                    break
                await asyncio.to_thread(self.tick)
        except asyncio.CancelledError:
            self._stopped.set()
            raise

    def stop(self) -> None:
        """Ask the loop to exit after the current sleep or tick."""
        self._stopped.set()


def maybe_start_in_process_ticker(ctx: Any) -> InProcessTicker | None:
    """Start the accelerator only when the host and profile both opt in.

    Returns ``None`` when ``ctx.spawn_task`` is absent (plain loaders and
    validation), when the preference is off (the default), or when the store
    cannot be opened. Failures are swallowed deliberately: an accelerator must
    never stop the plugin from loading or the cron path from working.
    """
    spawn = getattr(ctx, "spawn_task", None)
    if not callable(spawn):
        return None
    try:
        connection = open_profile_store()
    except (sqlite3.Error, OSError):
        return None
    try:
        enabled, interval = ticker_enabled(connection)
    except (sqlite3.Error, OSError):
        return None
    finally:
        connection.close()
    if not enabled:
        return None

    ticker = InProcessTicker(tick=run_tick, interval_seconds=max(5, interval), name=DEFAULT_NAME)
    spawn(ticker.run(), name=DEFAULT_NAME)
    return ticker
