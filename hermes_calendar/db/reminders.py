"""SQL for the notification-owned tables.

All SQL for reminder instances, deliveries, channels, and read/write prefs lives
here so the rest of the package never opens a cursor (``CLAUDE.md``). The engine
in :mod:`hermes_calendar.reminders` supplies the policy; this module supplies
transaction boundaries, claim/lease primitives, and row mapping.

Claiming is deliberately one ``BEGIN IMMEDIATE`` transaction: the conditional
update is what makes the cron tick, a dashboard "run now", and the optional
in-process ticker mutually exclusive (``app-notifications.md`` §6.4).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from ..models import new_uuid7
from .errors import NotFoundError

InstanceState = Literal[
    "scheduled",
    "firing",
    "retrying",
    "delivered",
    "snoozed",
    "acknowledged",
    "suppressed",
    "cancelled",
    "failed",
    "dead_letter",
]
Outcome = Literal["success", "transient", "permanent", "unknown"]

ACTIVE_STATES: tuple[InstanceState, ...] = ("scheduled", "retrying", "snoozed")
DELIVERABLE_STATES: tuple[InstanceState, ...] = ("scheduled", "retrying")
STATE_VALUES: frozenset[str] = frozenset(
    {
        "scheduled",
        "firing",
        "retrying",
        "delivered",
        "snoozed",
        "acknowledged",
        "suppressed",
        "cancelled",
        "failed",
        "dead_letter",
    }
)


class NotificationNotFoundError(NotFoundError):
    """No reminder instance with the requested id exists in this profile."""


@dataclass(frozen=True)
class ReminderDefinition:
    """One ``event_reminders`` row (the core-owned VALARM definition)."""

    id: str
    action: str
    related: str
    trigger_kind: str
    offset_seconds: int | None
    absolute_utc: int | None
    repeat_count: int
    repeat_interval_seconds: int | None


@dataclass(frozen=True)
class EventDefinition:
    """The core event fields materialization and rendering need.

    Descriptions, attendee rows, URLs, and conferencing data are deliberately
    absent: the notification layer must not need them to build a minimal chat
    message (``app-notifications.md`` §6.10 and §6.13).
    """

    id: str
    summary: str
    status: str
    transparency: str
    start_utc: int | None
    end_utc: int | None
    start_local: str | None
    start_tz: str | None
    duration_seconds: int | None
    start_date: str | None
    end_date: str | None
    is_all_day: bool
    rrule: str | None
    rdate: tuple[str, ...]
    exdate: tuple[str, ...]
    parent_id: str | None
    recurrence_key: str | None
    xprops: dict[str, Any]
    reminders: tuple[ReminderDefinition, ...]


@dataclass(frozen=True)
class InstanceDraft:
    """A materialized reminder occurrence ready to insert."""

    store_id: str
    event_id: str
    reminder_id: str
    recurrence_key: str
    occurrence_start_utc: int
    offset_seconds: int
    fire_at_utc: int
    priority: str
    channel_policy: dict[str, Any]
    dedup_key: str


@dataclass(frozen=True)
class ReminderInstance:
    """One ``reminder_instances`` row."""

    id: str
    store_id: str
    event_id: str
    reminder_id: str
    recurrence_key: str
    occurrence_start_utc: int
    offset_seconds: int
    fire_at_utc: int
    priority: str
    channel_policy: dict[str, Any]
    state: InstanceState
    attempt: int
    lease_until_utc: int | None
    snooze_until_utc: int | None
    dedup_key: str
    created_utc: int
    updated_utc: int


@dataclass(frozen=True)
class DeliveryRecord:
    """One ``notification_deliveries`` row."""

    id: int
    reminder_instance_id: str
    channel: str
    target: str
    attempt: int
    started_utc: int
    finished_utc: int | None
    outcome: Outcome
    exit_code: int | None
    provider_id: str | None
    error: str | None
    latency_ms: int | None


@dataclass(frozen=True)
class ChannelRow:
    """One configured delivery channel."""

    id: str
    kind: str
    enabled: bool
    config: dict[str, Any]


def dedup_key(store_id: str, event_id: str, reminder_id: str, recurrence_key: str) -> str:
    """Return the stable duplicate-delivery key for one reminder occurrence."""
    import hashlib

    raw = "\x1f".join((store_id, event_id, reminder_id, recurrence_key))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_now() -> int:
    return int(time.time())


def _load_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    loaded: Any = json.loads(str(value))
    return tuple(str(item) for item in loaded)


def _load_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    loaded: Any = json.loads(str(value))
    return {str(key): item for key, item in loaded.items()}


def _event_definition(
    row: sqlite3.Row, reminders: tuple[ReminderDefinition, ...]
) -> EventDefinition:
    return EventDefinition(
        id=str(row["id"]),
        summary=str(row["summary"]),
        status=str(row["status"]),
        transparency=str(row["transparency"]),
        start_utc=None if row["start_utc"] is None else int(row["start_utc"]),
        end_utc=None if row["end_utc"] is None else int(row["end_utc"]),
        start_local=None if row["start_local"] is None else str(row["start_local"]),
        start_tz=None if row["start_tz"] is None else str(row["start_tz"]),
        duration_seconds=(
            None if row["duration_seconds"] is None else int(row["duration_seconds"])
        ),
        start_date=None if row["start_date"] is None else str(row["start_date"]),
        end_date=None if row["end_date"] is None else str(row["end_date"]),
        is_all_day=bool(row["is_all_day"]),
        rrule=None if row["rrule"] is None else str(row["rrule"]),
        rdate=_load_strings(row["rdate_json"]),
        exdate=_load_strings(row["exdate_json"]),
        parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
        recurrence_key=None if row["recurrence_key"] is None else str(row["recurrence_key"]),
        xprops=_load_object(row["xprops_json"]),
        reminders=reminders,
    )


def _instance_from_row(row: sqlite3.Row) -> ReminderInstance:
    state = str(row["state"])
    if state not in STATE_VALUES:
        raise ValueError(f"unknown reminder state in database: {state!r}")
    return ReminderInstance(
        id=str(row["id"]),
        store_id=str(row["store_id"]),
        event_id=str(row["event_id"]),
        reminder_id=str(row["reminder_id"]),
        recurrence_key=str(row["recurrence_key"]),
        occurrence_start_utc=int(row["occurrence_start_utc"]),
        offset_seconds=int(row["offset_seconds"]),
        fire_at_utc=int(row["fire_at_utc"]),
        priority=str(row["priority"]),
        channel_policy=_load_object(row["channel_policy_json"]),
        state=cast(InstanceState, state),
        attempt=int(row["attempt"]),
        lease_until_utc=(None if row["lease_until_utc"] is None else int(row["lease_until_utc"])),
        snooze_until_utc=(
            None if row["snooze_until_utc"] is None else int(row["snooze_until_utc"])
        ),
        dedup_key=str(row["dedup_key"]),
        created_utc=int(row["created_utc"]),
        updated_utc=int(row["updated_utc"]),
    )


def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=int(row["id"]),
        reminder_instance_id=str(row["reminder_instance_id"]),
        channel=str(row["channel"]),
        target=str(row["target"]),
        attempt=int(row["attempt"]),
        started_utc=int(row["started_utc"]),
        finished_utc=None if row["finished_utc"] is None else int(row["finished_utc"]),
        outcome=cast(Outcome, str(row["outcome"])),
        exit_code=None if row["exit_code"] is None else int(row["exit_code"]),
        provider_id=None if row["provider_id"] is None else str(row["provider_id"]),
        error=None if row["error"] is None else str(row["error"]),
        latency_ms=None if row["latency_ms"] is None else int(row["latency_ms"]),
    )


class ReminderStore:
    """Notification-table operations over one opened profile connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection; callers must not write SQL through it."""
        return self._connection

    # ----------------------------------------------------------- store identity

    def identity(self) -> tuple[str, str, str]:
        """Return ``(store_scope, store_id, owner_profile_id)`` from schema_meta."""
        rows = self._connection.execute(
            "SELECT key, value FROM schema_meta WHERE key IN "
            "('store_scope', 'store_id', 'owner_profile_id')"
        ).fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        return (
            values.get("store_scope", ""),
            values.get("store_id", ""),
            values.get("owner_profile_id", ""),
        )

    # ------------------------------------------------------------- definitions

    def event_definitions(self) -> list[EventDefinition]:
        """Return master events with their reminder definitions."""
        rows = self._connection.execute(
            """
            SELECT e.*,
                   r.id AS reminder_id, r.action AS reminder_action,
                   r.related AS reminder_related,
                   r.trigger_kind AS reminder_trigger_kind,
                   r.offset_seconds AS reminder_offset_seconds,
                   r.absolute_utc AS reminder_absolute_utc,
                   r.repeat_count AS reminder_repeat_count,
                   r.repeat_interval_seconds AS reminder_repeat_interval
              FROM events e
              JOIN event_reminders r ON r.event_id = e.id
             WHERE e.deleted_utc IS NULL
               AND e.parent_id IS NULL
             ORDER BY e.id, r.rowid
            """
        ).fetchall()

        grouped: dict[str, tuple[sqlite3.Row, list[ReminderDefinition]]] = {}
        for row in rows:
            event_id = str(row["id"])
            if event_id not in grouped:
                grouped[event_id] = (row, [])
            grouped[event_id][1].append(
                ReminderDefinition(
                    id=str(row["reminder_id"]),
                    action=str(row["reminder_action"]),
                    related=str(row["reminder_related"]),
                    trigger_kind=str(row["reminder_trigger_kind"]),
                    offset_seconds=(
                        None
                        if row["reminder_offset_seconds"] is None
                        else int(row["reminder_offset_seconds"])
                    ),
                    absolute_utc=(
                        None
                        if row["reminder_absolute_utc"] is None
                        else int(row["reminder_absolute_utc"])
                    ),
                    repeat_count=int(row["reminder_repeat_count"]),
                    repeat_interval_seconds=(
                        None
                        if row["reminder_repeat_interval"] is None
                        else int(row["reminder_repeat_interval"])
                    ),
                )
            )
        return [_event_definition(row, tuple(reminders)) for row, reminders in grouped.values()]

    def override_definitions(self) -> list[EventDefinition]:
        """Return non-deleted override rows grouped by their parent at the caller."""
        rows = self._connection.execute(
            """
            SELECT * FROM events
             WHERE parent_id IS NOT NULL
               AND deleted_utc IS NULL
             ORDER BY parent_id, recurrence_key
            """
        ).fetchall()
        return [_event_definition(row, ()) for row in rows]

    def get_event(self, event_id: str) -> EventDefinition | None:
        """Return one master event (with reminder definitions) or ``None``."""
        row = self._connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        return _event_definition(row, ())

    # ---------------------------------------------------------- materialization

    def apply_materialization(
        self,
        store_id: str,
        drafts: list[InstanceDraft],
        *,
        window_end_utc: int,
        now: int,
    ) -> int:
        """Insert new instances and cancel stale active ones for one store.

        Returns the number of stale rows cancelled. Terminal rows (delivered,
        acknowledged, suppressed, cancelled, dead_letter) are never rewritten:
        they are the delivery history.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for draft in drafts:
                self._connection.execute(
                    """
                    INSERT INTO reminder_instances
                      (id, store_id, event_id, reminder_id, recurrence_key,
                       occurrence_start_utc, offset_seconds, fire_at_utc, priority,
                       channel_policy_json, state, attempt, lease_until_utc,
                       snooze_until_utc, dedup_key, created_utc, updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 0, NULL,
                            NULL, ?, ?, ?)
                    ON CONFLICT (dedup_key) DO NOTHING
                    """,
                    (
                        f"rmi_{new_uuid7()}",
                        draft.store_id,
                        draft.event_id,
                        draft.reminder_id,
                        draft.recurrence_key,
                        draft.occurrence_start_utc,
                        draft.offset_seconds,
                        draft.fire_at_utc,
                        draft.priority,
                        json.dumps(draft.channel_policy, sort_keys=True),
                        draft.dedup_key,
                        now,
                        now,
                    ),
                )

            self._connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _keep_dedup_keys (dedup_key TEXT PRIMARY KEY)"
            )
            self._connection.execute("DELETE FROM _keep_dedup_keys")
            self._connection.executemany(
                "INSERT OR IGNORE INTO _keep_dedup_keys (dedup_key) VALUES (?)",
                ((draft.dedup_key,) for draft in drafts),
            )
            cursor = self._connection.execute(
                """
                UPDATE reminder_instances
                   SET state = 'cancelled', updated_utc = ?
                 WHERE store_id = ?
                   AND state IN ('scheduled', 'retrying', 'snoozed')
                   AND occurrence_start_utc <= ?
                   AND dedup_key NOT IN (SELECT dedup_key FROM _keep_dedup_keys)
                """,
                (now, store_id, window_end_utc),
            )
            cancelled = cursor.rowcount
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return cancelled

    # --------------------------------------------------------------- claim path

    def claim_due(
        self,
        now: int,
        *,
        store_ids: tuple[str, ...],
        lease_seconds: int,
        limit: int = 200,
    ) -> list[ReminderInstance]:
        """Atomically claim due instances for the supplied stores."""
        if not store_ids:
            return []
        placeholders = ", ".join("?" for _ in store_ids)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self._connection.execute(
                f"""
                SELECT * FROM reminder_instances
                 WHERE store_id IN ({placeholders})
                   AND state IN ('scheduled', 'retrying')
                   AND fire_at_utc <= ?
                   AND (lease_until_utc IS NULL OR lease_until_utc < ?)
                   AND (snooze_until_utc IS NULL OR snooze_until_utc <= ?)
                 ORDER BY fire_at_utc, id
                 LIMIT ?
                """,
                (*store_ids, now, now, now, limit),
            ).fetchall()
            claimed: list[ReminderInstance] = []
            for row in rows:
                cursor = self._connection.execute(
                    """
                    UPDATE reminder_instances
                       SET state = 'firing', lease_until_utc = ?,
                           attempt = attempt + 1, updated_utc = ?
                     WHERE id = ?
                       AND state IN ('scheduled', 'retrying')
                       AND (lease_until_utc IS NULL OR lease_until_utc < ?)
                    """,
                    (now + lease_seconds, now, str(row["id"]), now),
                )
                if cursor.rowcount == 1:
                    snapshotted = _instance_from_row(row)
                    claimed.append(
                        replace(
                            snapshotted,
                            state="firing",
                            attempt=snapshotted.attempt + 1,
                            lease_until_utc=now + lease_seconds,
                            updated_utc=now,
                        )
                    )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return claimed

    def defer_quiet_hours(
        self,
        now: int,
        *,
        until_utc: int,
        priorities: tuple[str, ...],
        store_ids: tuple[str, ...],
    ) -> int:
        """Push quiet-hours-deferred instances to the end of the window."""
        if not store_ids or not priorities:
            return 0
        store_sql = ", ".join("?" for _ in store_ids)
        priority_sql = ", ".join("?" for _ in priorities)
        cursor = self._connection.execute(
            f"""
            UPDATE reminder_instances
               SET snooze_until_utc = ?, updated_utc = ?
             WHERE store_id IN ({store_sql})
               AND state IN ('scheduled', 'retrying')
               AND fire_at_utc <= ?
               AND (snooze_until_utc IS NULL OR snooze_until_utc <= ?)
               AND priority IN ({priority_sql})
            """,
            (until_utc, now, *store_ids, now, now, *priorities),
        )
        return cursor.rowcount

    def recover_expired_leases(self, now: int, *, store_ids: tuple[str, ...]) -> int:
        """Re-route rows stuck in ``firing`` after their lease expired.

        The attempted delivery is recorded as ``unknown`` so the dashboard can
        show that an attempt may have reached the user. Global duplicates are
        additionally blocked by the shared ledger before the retry is claimed.
        """
        if not store_ids:
            return 0
        placeholders = ", ".join("?" for _ in store_ids)
        rows = self._connection.execute(
            f"""
            SELECT * FROM reminder_instances
             WHERE store_id IN ({placeholders})
               AND state = 'firing'
               AND lease_until_utc IS NOT NULL
               AND lease_until_utc < ?
            """,
            (*store_ids, now),
        ).fetchall()
        if not rows:
            return 0
        for row in rows:
            self._record_delivery_raw(
                reminder_instance_id=str(row["id"]),
                channel="recovery",
                target="",
                attempt=int(row["attempt"]),
                started_utc=now,
                finished_utc=now,
                outcome="unknown",
                exit_code=None,
                provider_id=None,
                error="lease expired before delivery completed",
                latency_ms=0,
            )
            self._transition(
                str(row["id"]),
                state="retrying",
                now=now,
                snooze_until_utc=now,
            )
        return len(rows)

    # -------------------------------------------------------------- transitions

    def _transition(
        self,
        instance_id: str,
        *,
        state: InstanceState,
        now: int,
        snooze_until_utc: int | None = None,
    ) -> None:
        self._connection.execute(
            """
            UPDATE reminder_instances
               SET state = ?, lease_until_utc = NULL, snooze_until_utc = ?,
                   updated_utc = ?
             WHERE id = ?
            """,
            (state, snooze_until_utc, now, instance_id),
        )

    def mark_delivered(self, instance_id: str, now: int) -> None:
        """Finish an instance as delivered and clear its lease and snooze."""
        self._transition(instance_id, state="delivered", now=now)

    def mark_suppressed(self, instance_id: str, now: int) -> None:
        """Finish a stale instance without delivering it."""
        self._transition(instance_id, state="suppressed", now=now)

    def mark_cancelled(self, instance_id: str, now: int) -> None:
        """Cancel an instance whose event or reminder definition vanished."""
        self._transition(instance_id, state="cancelled", now=now)

    def mark_failed(self, instance_id: str, now: int) -> None:
        """Finish an instance whose retry budget is exhausted."""
        self._transition(instance_id, state="failed", now=now)

    def requeue(self, instance_id: str, *, next_attempt_utc: int, now: int) -> None:
        """Schedule one transient retry at ``next_attempt_utc``."""
        self._transition(
            instance_id,
            state="retrying",
            now=now,
            snooze_until_utc=next_attempt_utc,
        )

    def snooze(self, instance_id: str, *, until_utc: int, now: int) -> None:
        """Snooze a reminder so a later tick delivers it again."""
        row = self.get_instance(instance_id)
        if row.state == "acknowledged":
            raise NotificationNotFoundError(f"notification {instance_id} is dismissed")
        self._connection.execute(
            """
            UPDATE reminder_instances
               SET state = 'scheduled', lease_until_utc = NULL,
                   snooze_until_utc = ?, attempt = 0, updated_utc = ?
             WHERE id = ?
            """,
            (until_utc, now, instance_id),
        )

    def dismiss(self, instance_id: str, now: int) -> None:
        """Acknowledge and hide a reminder; dismissing twice is a no-op."""
        self._connection.execute(
            """
            UPDATE reminder_instances
               SET state = 'acknowledged', lease_until_utc = NULL,
                   snooze_until_utc = NULL, updated_utc = ?
             WHERE id = ?
            """,
            (now, instance_id),
        )

    # ---------------------------------------------------------------- deliveries

    def _record_delivery_raw(
        self,
        *,
        reminder_instance_id: str,
        channel: str,
        target: str,
        attempt: int,
        started_utc: int,
        finished_utc: int | None,
        outcome: Outcome,
        exit_code: int | None,
        provider_id: str | None,
        error: str | None,
        latency_ms: int | None,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO notification_deliveries
              (reminder_instance_id, channel, target, attempt, started_utc,
               finished_utc, outcome, exit_code, provider_id, error, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reminder_instance_id,
                channel,
                target,
                attempt,
                started_utc,
                finished_utc,
                outcome,
                exit_code,
                provider_id,
                error,
                latency_ms,
            ),
        )
        return int(cursor.lastrowid or 0)

    def record_delivery(
        self,
        *,
        reminder_instance_id: str,
        channel: str,
        target: str,
        attempt: int,
        started_utc: int,
        finished_utc: int,
        outcome: Outcome,
        exit_code: int | None = None,
        provider_id: str | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> int:
        """Persist one delivery attempt with a redacted error string."""
        return self._record_delivery_raw(
            reminder_instance_id=reminder_instance_id,
            channel=channel,
            target=target,
            attempt=attempt,
            started_utc=started_utc,
            finished_utc=finished_utc,
            outcome=outcome,
            exit_code=exit_code,
            provider_id=provider_id,
            error=error,
            latency_ms=latency_ms,
        )

    def deliveries_for(self, instance_id: str) -> list[DeliveryRecord]:
        """Return the delivery log for one instance, oldest first."""
        rows = self._connection.execute(
            """
            SELECT * FROM notification_deliveries
             WHERE reminder_instance_id = ?
             ORDER BY id
            """,
            (instance_id,),
        ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def latest_delivery(self, instance_id: str) -> DeliveryRecord | None:
        """Return the most recent delivery attempt for one instance."""
        row = self._connection.execute(
            """
            SELECT * FROM notification_deliveries
             WHERE reminder_instance_id = ?
             ORDER BY id DESC
             LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
        return None if row is None else _delivery_from_row(row)

    # ------------------------------------------------------------------- reads

    def get_instance(self, instance_id: str) -> ReminderInstance:
        """Return one instance or raise :class:`NotificationNotFoundError`."""
        row = self._connection.execute(
            "SELECT * FROM reminder_instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise NotificationNotFoundError(f"notification {instance_id} not found")
        return _instance_from_row(row)

    def list_instances(
        self,
        *,
        states: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[ReminderInstance]:
        """Return instances newest-first, optionally filtered by state."""
        if states is None:
            rows = self._connection.execute(
                "SELECT * FROM reminder_instances ORDER BY fire_at_utc DESC, id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            placeholders = ", ".join("?" for _ in states)
            rows = self._connection.execute(
                f"""
                SELECT * FROM reminder_instances
                 WHERE state IN ({placeholders})
                 ORDER BY fire_at_utc DESC, id
                 LIMIT ?
                """,
                (*states, limit),
            ).fetchall()
        return [_instance_from_row(row) for row in rows]

    # ------------------------------------------------------- preferences/channels

    def get_pref(self, key: str) -> Any | None:
        """Return a decoded preference value, or ``None`` when unset."""
        row = self._connection.execute(
            "SELECT value_json FROM notification_prefs WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        loaded: Any = json.loads(str(row["value_json"]))
        return loaded

    def set_pref(self, key: str, value: Any, now: int | None = None) -> None:
        """Upsert a JSON preference value."""
        timestamp = _utc_now() if now is None else now
        self._connection.execute(
            """
            INSERT INTO notification_prefs (key, value_json, updated_utc)
            VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE
              SET value_json = excluded.value_json, updated_utc = excluded.updated_utc
            """,
            (key, json.dumps(value, sort_keys=True), timestamp),
        )

    def channels(self) -> list[ChannelRow]:
        """Return configured channels ordered with in-app first."""
        rows = self._connection.execute(
            "SELECT * FROM notification_channels ORDER BY id"
        ).fetchall()
        return [
            ChannelRow(
                id=str(row["id"]),
                kind=str(row["kind"]),
                enabled=bool(row["enabled"]),
                config=_load_object(row["config_json"]),
            )
            for row in rows
        ]

    def set_channel(self, channel_id: str, *, enabled: bool, config: dict[str, Any]) -> None:
        """Enable/disable a channel and replace its config (tests and dashboard)."""
        cursor = self._connection.execute(
            """
            UPDATE notification_channels
               SET enabled = ?, config_json = ?
             WHERE id = ?
            """,
            (int(enabled), json.dumps(config, sort_keys=True), channel_id),
        )
        if cursor.rowcount != 1:
            raise NotificationNotFoundError(f"channel {channel_id} not found")
