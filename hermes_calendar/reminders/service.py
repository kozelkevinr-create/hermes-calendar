"""Read and mutate notifications for the agent tools and future dashboard API.

These helpers are deliberately small: they resolve a profile-local instance,
look up only the event summary needed for display, and never expose a
description, location, attendee, or delivery body. They are also the shared
implementation the dashboard's ``plugin_api.py`` can call later.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime

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
SNOOZE_MINUTES = frozenset({5, 10, 60})


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
    state: str | None = None,
) -> list[dict[str, object]]:
    """Return notification payloads newest-first."""
    profile_store = ReminderStore(profile_connection)
    global_store = None if global_connection is None else ReminderStore(global_connection)
    if state is not None:
        states: tuple[str, ...] | None = (state,)
    elif include_dismissed:
        states = None
    else:
        states = VISIBLE_STATES
    instances = profile_store.list_instances(states=states, limit=limit)
    return [notification_payload(instance, profile_store, global_store) for instance in instances]


def snooze_notification(
    profile_connection: sqlite3.Connection,
    *,
    notification_id: str,
    minutes: int,
    global_connection: sqlite3.Connection | None = None,
    now: int | None = None,
) -> dict[str, object]:
    """Snooze a notification for 5, 10, or 60 minutes."""
    if minutes not in SNOOZE_MINUTES:
        raise ValueError("minutes must be one of 5, 10, 60")
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
