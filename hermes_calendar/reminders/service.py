"""Read and mutate notifications for the agent tools and future dashboard API.

These helpers are deliberately small: they resolve a profile-local instance,
look up only the event summary needed for display, and never expose a
description, location, attendee, or delivery body. They are also the shared
implementation the dashboard's ``plugin_api.py`` can call later.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from ..db import DatabaseError, open_global_store, open_profile_store
from ..db.reminders import DeliveryRecord, ReminderInstance, ReminderStore
from .render import sanitize

VISIBLE_STATES: tuple[str, ...] = (
    "scheduled",
    "firing",
    "retrying",
    "delivered",
    "snoozed",
    "suppressed",
    "failed",
    "dead_letter",
)
SNOOZE_MINUTES = frozenset({5, 15, 60, 1440})


@contextmanager
def notification_stores() -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection | None]]:
    """Open the profile store and, best-effort, the shared global store.

    A broken or unreadable global store must not hide profile-local
    notifications; callers receive ``None`` and can still list and mutate the
    profile rows.
    """
    profile_connection = open_profile_store()
    global_connection: sqlite3.Connection | None = None
    try:
        try:
            global_connection = open_global_store()
        except (DatabaseError, sqlite3.Error, OSError):
            global_connection = None
        yield profile_connection, global_connection
    finally:
        if global_connection is not None:
            global_connection.close()
        profile_connection.close()


def _states_for(*, include_dismissed: bool, state: str | None) -> tuple[str, ...] | None:
    """Resolve the state filter shared by list and count operations."""
    if state is not None:
        if state not in VISIBLE_STATES:
            raise ValueError(
                "state must be one of " + ", ".join(repr(item) for item in VISIBLE_STATES)
            )
        return (state,)
    if include_dismissed:
        return None
    return VISIBLE_STATES


def _rfc3339(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _delivery_payload(record: DeliveryRecord) -> dict[str, object]:
    return {
        "channel": record.channel,
        "attempt": record.attempt,
        "outcome": record.outcome,
        "exit_code": record.exit_code,
        "provider_id": record.provider_id,
        "error": record.error,
        "latency_ms": record.latency_ms,
        "started": _rfc3339(record.started_utc),
        "finished": None if record.finished_utc is None else _rfc3339(record.finished_utc),
    }


def _summary_for(
    instance: ReminderInstance,
    profile_store: ReminderStore,
    global_store: ReminderStore | None,
) -> str:
    store = global_store if instance.store_id == "global" else profile_store
    if store is not None:
        event = store.get_event(instance.event_id)
        if event is not None:
            return sanitize(event.summary, cap=200) or "Calendar reminder"
    return "Calendar reminder"


def notification_payload(
    instance: ReminderInstance,
    profile_store: ReminderStore,
    global_store: ReminderStore | None,
    *,
    include_deliveries: bool = True,
) -> dict[str, object]:
    """Serialize one notification without leaking event details."""
    payload: dict[str, object] = {
        "id": instance.id,
        "title": _summary_for(instance, profile_store, global_store),
        "state": instance.state,
        "priority": instance.priority,
        "store_id": instance.store_id,
        "event_id": instance.event_id,
        "recurrence_key": instance.recurrence_key,
        "occurrence_start": _rfc3339(instance.occurrence_start_utc),
        "fire_at": _rfc3339(instance.fire_at_utc),
        "snooze_until": (
            None if instance.snooze_until_utc is None else _rfc3339(instance.snooze_until_utc)
        ),
        "attempt": instance.attempt,
    }
    if include_deliveries:
        latest = profile_store.latest_delivery(instance.id)
        payload["last_delivery"] = None if latest is None else _delivery_payload(latest)
    return payload


def list_notifications(
    profile_connection: sqlite3.Connection,
    *,
    global_connection: sqlite3.Connection | None = None,
    include_dismissed: bool = False,
    limit: int = 20,
    offset: int = 0,
    state: str | None = None,
) -> list[dict[str, object]]:
    """Return one page of notification payloads newest-first."""
    profile_store = ReminderStore(profile_connection)
    global_store = None if global_connection is None else ReminderStore(global_connection)
    states = _states_for(include_dismissed=include_dismissed, state=state)
    instances = profile_store.list_instances(states=states, limit=limit, offset=offset)
    return [notification_payload(instance, profile_store, global_store) for instance in instances]


def count_notifications(
    profile_connection: sqlite3.Connection,
    *,
    include_dismissed: bool = False,
    state: str | None = None,
) -> int:
    """Count the notification rows the same filters would page through."""
    states = _states_for(include_dismissed=include_dismissed, state=state)
    return ReminderStore(profile_connection).count_instances(states=states)


def notification_page(
    profile_connection: sqlite3.Connection,
    *,
    global_connection: sqlite3.Connection | None = None,
    include_dismissed: bool = False,
    limit: int = 20,
    offset: int = 0,
    state: str | None = None,
) -> dict[str, object]:
    """Return a paginated notification page with its unpaged total."""
    notifications = list_notifications(
        profile_connection,
        global_connection=global_connection,
        include_dismissed=include_dismissed,
        limit=limit,
        offset=offset,
        state=state,
    )
    total = count_notifications(
        profile_connection,
        include_dismissed=include_dismissed,
        state=state,
    )
    return {
        "count": len(notifications),
        "total": total,
        "limit": limit,
        "offset": offset,
        "notifications": notifications,
    }


def snooze_notification(
    profile_connection: sqlite3.Connection,
    *,
    notification_id: str,
    minutes: int,
    global_connection: sqlite3.Connection | None = None,
    now: int | None = None,
) -> dict[str, object]:
    """Snooze a notification for one of the allowlisted durations."""
    if minutes not in SNOOZE_MINUTES:
        allowed = ", ".join(str(item) for item in sorted(SNOOZE_MINUTES))
        raise ValueError(f"minutes must be one of {allowed}")
    timestamp = int(time.time()) if now is None else int(now)
    store = ReminderStore(profile_connection)
    store.snooze(notification_id, until_utc=timestamp + minutes * 60, now=timestamp)
    instance = store.get_instance(notification_id)
    global_store = None if global_connection is None else ReminderStore(global_connection)
    return notification_payload(instance, store, global_store)


def dismiss_notification(
    profile_connection: sqlite3.Connection,
    *,
    notification_id: str,
    global_connection: sqlite3.Connection | None = None,
    now: int | None = None,
) -> dict[str, object]:
    """Dismiss (acknowledge) a notification; repeated dismisses are no-ops."""
    timestamp = int(time.time()) if now is None else int(now)
    store = ReminderStore(profile_connection)
    store.dismiss(notification_id, timestamp)
    instance = store.get_instance(notification_id)
    global_store = None if global_connection is None else ReminderStore(global_connection)
    return notification_payload(instance, store, global_store)
