"""Value objects shared by the reminder engine and its adapters.

The shapes mirror ``docs/design/app-notifications.md`` §6.6 so a post-MVP adapter
can implement the same ``Notifier`` protocol without changing the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Literal

Priority = Literal["digest", "normal", "high", "critical"]
Outcome = Literal["success", "transient", "permanent", "unknown"]

PRIORITIES: frozenset[str] = frozenset({"digest", "normal", "high", "critical"})


@dataclass(frozen=True)
class Notification:
    """One rendered reminder ready for a channel adapter."""

    id: str
    title: str
    body: str
    priority: Priority
    store_id: str
    event_id: str
    occurrence_utc: int
    deep_link: str | None = None
    media_path: Path | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """The adapter's outcome for one attempt.

    ``detail`` is already redacted: it must never contain a message body, event
    description, location, attendee, token, or chat id.
    """

    outcome: Outcome
    provider_id: str | None = None
    detail: str | None = None
    latency_ms: int = 0
    code: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class Availability:
    """Whether a channel can be used right now, with a non-secret reason."""

    available: bool
    detail: str | None = None


@dataclass(frozen=True)
class QuietHours:
    """The local-time quiet window and the priorities allowed through it."""

    start: time
    end: time
    allow: frozenset[str] = field(default_factory=lambda: frozenset({"high", "critical"}))

    def contains(self, local: time) -> bool:
        """Return whether ``local`` falls inside the window (end exclusive)."""
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


@dataclass(frozen=True)
class NotificationSettings:
    """Profile-scoped notification policy resolved from ``notification_prefs``."""

    timezone: str = "UTC"
    stale_grace_minutes: int = 10
    max_late_minutes: int = 60
    materialize_horizon_days: int = 90
    all_day_fire_time: time = time(7, 0)
    quiet_hours: QuietHours | None = None
    dashboard_url: str | None = None
    max_attempts: int = 5
    attempt_lease_seconds: int = 120
    global_lease_ttl_seconds: int = 180
    global_takeover_grace_seconds: int = 60
    backoff_seconds: tuple[int, ...] = (10, 30, 120, 600, 1800)
    jitter_ratio: float = 0.2
    in_process_ticker_enabled: bool = False
    ticker_interval_seconds: int = 30


@dataclass(frozen=True)
class TickResult:
    """What one tick did; the CLI renders it and tests assert on it."""

    profile_id: str
    global_lease_holder: bool = False
    claimed: int = 0
    delivered: int = 0
    retrying: int = 0
    suppressed: int = 0
    cancelled: int = 0
    failed: int = 0
    duplicates_skipped: int = 0
    quiet_deferred: int = 0
    materialization_errors: int = 0
    clock_backwards: bool = False
    global_error: str | None = None
