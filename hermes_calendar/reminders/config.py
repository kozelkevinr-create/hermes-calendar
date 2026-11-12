"""Load notification settings from ``notification_prefs``.

The dashboard owns editing; this module is the fail-closed reader. A malformed
value falls back to the documented default rather than stopping the tick, but a
missing ``hermes_send`` target never falls back to an inferred one: an explicit
target is required before the adapter is considered available.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any

from ..db.reminders import ReminderStore
from .models import NotificationSettings, QuietHours

PREF_KEY = "notifications"


def _int(value: Any, default: int, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _time(value: Any, default: time) -> time:
    if not isinstance(value, str):
        return default
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(int(hour_text), int(minute_text))
    except (ValueError, TypeError):
        return default


def _quiet_hours(value: Any) -> QuietHours | None:
    if not isinstance(value, Mapping):
        return None
    start = _time(value.get("start"), time(21, 30))
    end = _time(value.get("end"), time(7, 0))
    allow_raw = value.get("allow")
    if isinstance(allow_raw, list):
        allow = frozenset(str(item) for item in allow_raw)
    else:
        allow = frozenset({"high", "critical"})
    return QuietHours(start=start, end=end, allow=allow)


def settings_from_mapping(data: Mapping[str, Any]) -> NotificationSettings:
    """Build settings from the documented YAML-shaped mapping."""
    dashboard = data.get("dashboard")
    dashboard_url: str | None = None
    if isinstance(dashboard, Mapping) and isinstance(dashboard.get("public_url"), str):
        dashboard_url = str(dashboard["public_url"]).rstrip("/") or None
    elif isinstance(data.get("dashboard_url"), str):
        dashboard_url = str(data["dashboard_url"]).rstrip("/") or None

    backoff_raw = data.get("backoff_seconds")
    if isinstance(backoff_raw, list) and backoff_raw:
        backoff = tuple(
            _int(item, 30, minimum=1) for item in backoff_raw if not isinstance(item, bool)
        )
    else:
        backoff = (10, 30, 120, 600, 1800)

    ticker = data.get("in_process_ticker")
    ticker_mapping = ticker if isinstance(ticker, Mapping) else {}

    return NotificationSettings(
        timezone=str(data.get("timezone") or "UTC"),
        stale_grace_minutes=_int(data.get("stale_grace_minutes"), 10),
        max_late_minutes=_int(data.get("max_late_minutes"), 60),
        materialize_horizon_days=_int(data.get("materialize_horizon_days"), 90, minimum=1),
        all_day_fire_time=_time(data.get("all_day_fire_time"), time(7, 0)),
        quiet_hours=_quiet_hours(data.get("quiet_hours")),
        dashboard_url=dashboard_url,
        max_attempts=_int(data.get("max_attempts"), 5, minimum=1, maximum=20),
        attempt_lease_seconds=_int(data.get("attempt_lease_seconds"), 120, minimum=30),
        global_lease_ttl_seconds=_int(data.get("global_lease_ttl_seconds"), 180, minimum=60),
        global_takeover_grace_seconds=_int(
            data.get("global_takeover_grace_seconds"), 60, minimum=0
        ),
        backoff_seconds=backoff,
        jitter_ratio=float(data.get("jitter_ratio", 0.2))
        if isinstance(data.get("jitter_ratio"), (int, float))
        else 0.2,
        in_process_ticker_enabled=_bool(ticker_mapping.get("enabled"), False),
        ticker_interval_seconds=_int(ticker_mapping.get("interval_seconds"), 30, minimum=5),
    )


def load_settings(store: ReminderStore) -> NotificationSettings:
    """Read and validate the profile's notification preferences."""
    raw = store.get_pref(PREF_KEY)
    if not isinstance(raw, Mapping):
        return NotificationSettings()
    return settings_from_mapping(raw)
