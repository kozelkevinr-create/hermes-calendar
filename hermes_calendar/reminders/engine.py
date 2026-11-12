"""The reminder engine: materialize, claim, route, deliver, record.

One tick is a short-lived operation that the per-profile ``hermes cron`` job (or
an explicitly enabled in-process accelerator) calls. It is safe to run
concurrently: the profile-local claim uses ``BEGIN IMMEDIATE`` and a row lease,
and global-calendar delivery additionally requires the shared lease in the
global database (``app-notifications.md`` §6.2 and §6.4).

The engine is deliberately policy-heavy but I/O-light: SQL lives in
``hermes_calendar.db.reminders``, rendering in ``render.py``, and channel calls
behind the ``Notifier`` protocol. Tests inject a fake clock and fake runner.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import coordination
from ..db.reminders import (
    EventDefinition,
    InstanceDraft,
    ReminderDefinition,
    ReminderInstance,
    ReminderStore,
    dedup_key,
)
from ..recurrence import Occurrence, expand
from ..recurrence.types import MasterEvent, OverrideEvent
from .adapters import Notifier, build_chat_notifier
from .config import load_settings
from .models import NotificationSettings, TickResult
from .render import render_notification

_LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"
_CATCH_UP_PREFIX_SECONDS = 120


class ProfileMismatchError(RuntimeError):
    """The tick was pointed at a store belonging to another profile."""


class ReminderEngine:
    """Run reminder ticks for one profile."""

    def __init__(
        self,
        *,
        profile_connection: sqlite3.Connection,
        profile_id: str,
        global_connection: sqlite3.Connection | None = None,
        settings: NotificationSettings | None = None,
        chat_notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._profile = ReminderStore(profile_connection)
        self._global_connection = global_connection
        self._global = None if global_connection is None else ReminderStore(global_connection)
        self._profile_id = profile_id
        self._clock = clock
        self._rng = rng
        scope, store_id, owner = self._profile.identity()
        if scope != "profile" or owner != profile_id:
            raise ProfileMismatchError(
                f"refusing to tick profile {profile_id!r}: store scope={scope!r} "
                f"store_id={store_id!r} owner={owner!r}"
            )
        self._settings = load_settings(self._profile) if settings is None else settings
        self._chat: Notifier | None = (
            build_chat_notifier(self._profile.channels())
            if chat_notifier is None
            else chat_notifier
        )

    @property
    def settings(self) -> NotificationSettings:
        """The resolved settings for this engine."""
        return self._settings

    def tick(self, now: int | None = None) -> TickResult:
        """Run one complete tick and return counters suitable for diagnostics."""
        timestamp = int(self._clock()) if now is None else int(now)
        last_tick = self._profile.get_pref("last_tick_utc")
        if isinstance(last_tick, int) and timestamp < last_tick - 60:
            self._profile.set_pref("last_tick_utc", timestamp, timestamp)
            return TickResult(profile_id=self._profile_id, clock_backwards=True)

        global_error: str | None = None
        holder = False
        if self._global is not None:
            try:
                holder = coordination.acquire_or_renew_lease(
                    self._require_global_connection(),
                    profile_id=self._profile_id,
                    now=timestamp,
                    ttl_seconds=self._settings.global_lease_ttl_seconds,
                    takeover_grace_seconds=self._settings.global_takeover_grace_seconds,
                )
            except sqlite3.Error as exc:
                # Global delivery degrades; profile-local reminders still run.
                global_error = f"global coordination unavailable: {type(exc).__name__}"
                holder = False

        profile_store_id = f"profile:{self._profile_id}"
        claimed_store_ids: tuple[str, ...] = (profile_store_id,)
        if holder:
            claimed_store_ids = (profile_store_id, "global")

        counters: dict[str, int] = {
            "claimed": 0,
            "delivered": 0,
            "retrying": 0,
            "suppressed": 0,
            "cancelled": 0,
            "failed": 0,
            "duplicates_skipped": 0,
            "quiet_deferred": 0,
            "materialization_errors": 0,
        }

        try:
            self._profile.recover_expired_leases(timestamp, store_ids=claimed_store_ids)
            counters["materialization_errors"] += self._materialize(
                self._profile, profile_store_id, timestamp
            )
            if holder and self._global is not None:
                counters["materialization_errors"] += self._materialize(
                    self._global, "global", timestamp
                )

            quiet = self._quiet_window(timestamp)
            if quiet is not None:
                deferred, until = quiet
                counters["quiet_deferred"] = self._profile.defer_quiet_hours(
                    timestamp,
                    until_utc=until,
                    priorities=tuple(deferred),
                    store_ids=claimed_store_ids,
                )

            claimed = self._profile.claim_due(
                timestamp,
                store_ids=claimed_store_ids,
                lease_seconds=self._settings.attempt_lease_seconds,
            )
            counters["claimed"] = len(claimed)
            for instance in claimed:
                outcome = self._deliver(instance, timestamp, holder)
                counters[outcome] += 1
        finally:
            self._profile.set_pref("last_tick_utc", timestamp, timestamp)

        return TickResult(
            profile_id=self._profile_id,
            global_lease_holder=holder,
            claimed=counters["claimed"],
            delivered=counters["delivered"],
            retrying=counters["retrying"],
            suppressed=counters["suppressed"],
            cancelled=counters["cancelled"],
            failed=counters["failed"],
            duplicates_skipped=counters["duplicates_skipped"],
            quiet_deferred=counters["quiet_deferred"],
            materialization_errors=counters["materialization_errors"],
            global_error=global_error,
        )

    # -------------------------------------------------------------- materialize

    def _materialize(self, store: ReminderStore, store_id: str, now: int) -> int:
        """Expand masters and upsert reminder instances for one store."""
        errors = 0
        definitions = store.event_definitions()
        overrides_by_parent: dict[str, list[OverrideEvent]] = {}
        for override in store.override_definitions():
            if override.parent_id is None or override.recurrence_key is None:
                continue
            try:
                overrides_by_parent.setdefault(override.parent_id, []).append(
                    _to_override(override)
                )
            except ValueError:
                errors += 1

        window_start = now - 86_400
        window_end = now + self._settings.materialize_horizon_days * 86_400
        drafts: list[InstanceDraft] = []
        for definition in definitions:
            if definition.start_utc is None and not definition.is_all_day:
                continue
            try:
                master = _to_master(definition)
            except ValueError:
                errors += 1
                continue
            try:
                occurrences = expand(
                    master,
                    window_start,
                    window_end,
                    overrides_by_parent.get(definition.id, ()),
                )
            except (ValueError, OverflowError):
                errors += 1
                continue
            for occurrence in occurrences:
                for reminder in definition.reminders:
                    try:
                        drafts.extend(
                            _drafts_for(
                                definition,
                                occurrence,
                                reminder,
                                store_id=store_id,
                                settings=self._settings,
                            )
                        )
                    except (ValueError, OverflowError, ZoneInfoNotFoundError):
                        errors += 1

        self._profile.apply_materialization(store_id, drafts, window_end_utc=window_end, now=now)
        return errors

    # ------------------------------------------------------------------ deliver

    def _deliver(self, instance: ReminderInstance, now: int, holder: bool) -> str:
        """Deliver one claimed instance and return its counter bucket."""
        store = self._global if instance.store_id == "global" else self._profile
        if store is None or (instance.store_id == "global" and not holder):
            self._profile.mark_cancelled(instance.id, now)
            return "cancelled"

        event = store.get_event(instance.event_id)
        if event is None or event.status == "CANCELLED":
            self._profile.mark_cancelled(instance.id, now)
            return "cancelled"

        if instance.store_id == "global" and self._global_connection is not None:
            try:
                if coordination.ledger_contains(self._global_connection, instance.dedup_key):
                    self._profile.mark_delivered(instance.id, now)
                    return "duplicates_skipped"
            except sqlite3.Error:
                # The ledger is unavailable; at-most-once says do not risk a
                # duplicate on a global event.
                self._profile.mark_suppressed(instance.id, now)
                return "suppressed"

        grace = self._settings.stale_grace_minutes * 60
        # An END-related VALARM is evaluated against the occurrence end; the
        # generic start-plus-grace boundary would suppress a legitimate
        # "10 minutes before it ends" alarm.
        end_related = instance.channel_policy.get("related") == "END"
        if event.is_all_day:
            stale_after = event.end_utc or instance.occurrence_start_utc
        elif end_related:
            stale_after = instance.occurrence_start_utc + (event.duration_seconds or 0)
        else:
            stale_after = instance.occurrence_start_utc
        # A quiet-hours deferral (and a user snooze) moves the effective due
        # time; otherwise the catch-up limit would suppress exactly the
        # reminders quiet hours were supposed to hold until morning.
        effective_fire = max(instance.fire_at_utc, instance.snooze_until_utc or 0)
        too_late = (
            not event.is_all_day and now - effective_fire > self._settings.max_late_minutes * 60
        )
        if now > stale_after + grace or too_late:
            self._profile.mark_suppressed(instance.id, now)
            return "suppressed"

        late = now - effective_fire > _CATCH_UP_PREFIX_SECONDS
        notification = render_notification(
            instance,
            summary=event.summary,
            settings=self._settings,
            late=late,
        )

        # The in-app row is the source of truth and is recorded before any
        # external send. It never fails.
        self._profile.record_delivery(
            reminder_instance_id=instance.id,
            channel="in_app",
            target="in-app",
            attempt=instance.attempt,
            started_utc=now,
            finished_utc=now,
            outcome="success",
            latency_ms=0,
        )

        external_attempted = False
        if instance.priority != "digest" and self._chat is not None:
            availability = self._chat.available()
            if not availability.available:
                external_attempted = True
                self._profile.record_delivery(
                    reminder_instance_id=instance.id,
                    channel="hermes_send",
                    target=_target_of(self._chat),
                    attempt=instance.attempt,
                    started_utc=now,
                    finished_utc=now,
                    outcome="permanent",
                    error=f"channel unavailable: {availability.detail or 'unknown reason'}",
                    latency_ms=0,
                )
            else:
                external_attempted = True
                result = self._chat.send(notification)
                self._profile.record_delivery(
                    reminder_instance_id=instance.id,
                    channel="hermes_send",
                    target=_target_of(self._chat),
                    attempt=instance.attempt,
                    started_utc=now,
                    finished_utc=now,
                    outcome=result.outcome,
                    exit_code=result.exit_code,
                    provider_id=result.provider_id,
                    error=result.detail,
                    latency_ms=result.latency_ms,
                )
                if result.outcome in {"transient", "unknown"}:
                    if instance.attempt < self._settings.max_attempts:
                        next_attempt = now + self._backoff(instance.attempt)
                        self._profile.requeue(instance.id, next_attempt_utc=next_attempt, now=now)
                        return "retrying"
                    # Retry budget exhausted: fall back to the in-app record.
                    self._profile.mark_delivered(instance.id, now)
                    self._record_global_ledger(instance, now)
                    return "delivered"
                if result.outcome == "permanent":
                    self._profile.mark_delivered(instance.id, now)
                    self._record_global_ledger(instance, now)
                    return "delivered"

        self._profile.mark_delivered(instance.id, now)
        if not external_attempted or instance.store_id == "global":
            self._record_global_ledger(instance, now)
        return "delivered"

    def _record_global_ledger(self, instance: ReminderInstance, now: int) -> None:
        if instance.store_id != "global" or self._global_connection is None:
            return
        try:
            coordination.record_ledger(
                self._global_connection,
                dedup_key=instance.dedup_key,
                now=now,
                profile_id=self._profile_id,
            )
        except sqlite3.Error:
            # Delivery already happened; the lease is the primary defense.
            return

    def _backoff(self, attempt: int) -> int:
        index = min(max(attempt - 1, 0), len(self._settings.backoff_seconds) - 1)
        base = self._settings.backoff_seconds[index]
        jitter = self._settings.jitter_ratio
        factor = 1.0 + (self._rng() * 2.0 - 1.0) * jitter
        return max(1, int(base * factor))

    def _quiet_window(self, now: int) -> tuple[set[str], int] | None:
        quiet = self._settings.quiet_hours
        if quiet is None:
            return None
        zone = _zone(self._settings.timezone)
        local = datetime.fromtimestamp(now, zone)
        if not quiet.contains(local.time()):
            return None
        deferred = {"digest", "normal", "high", "critical"} - quiet.allow
        if not deferred:
            return None
        end_candidate = datetime.combine(local.date(), quiet.end)
        if quiet.start > quiet.end and local.time() >= quiet.start:
            end_candidate += timedelta(days=1)
        until = int(end_candidate.replace(tzinfo=zone, fold=0).timestamp())
        return deferred, until

    def _require_global_connection(self) -> sqlite3.Connection:
        assert self._global_connection is not None
        return self._global_connection


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _to_master(definition: EventDefinition) -> MasterEvent:
    if definition.is_all_day:
        if definition.start_date is None or definition.end_date is None:
            raise ValueError("all-day event is missing dates")
        return MasterEvent(
            start_date=date.fromisoformat(definition.start_date),
            end_date=date.fromisoformat(definition.end_date),
            rrule=definition.rrule,
            rdate=definition.rdate,
            exdate=definition.exdate,
        )
    if definition.start_local is None or definition.duration_seconds is None:
        raise ValueError("timed event is missing its local start or duration")
    return MasterEvent(
        start_local=datetime.strptime(definition.start_local, _LOCAL_FORMAT),
        start_tz=definition.start_tz,
        start_utc=(
            datetime.fromtimestamp(definition.start_utc, UTC)
            if definition.start_utc is not None
            else None
        ),
        duration_seconds=definition.duration_seconds,
        rrule=definition.rrule,
        rdate=definition.rdate,
        exdate=definition.exdate,
    )


def _to_override(definition: EventDefinition) -> OverrideEvent:
    assert definition.recurrence_key is not None
    if definition.is_all_day:
        if definition.start_date is None or definition.end_date is None:
            raise ValueError("all-day override is missing dates")
        return OverrideEvent(
            recurrence_key=definition.recurrence_key,
            start_date=date.fromisoformat(definition.start_date),
            end_date=date.fromisoformat(definition.end_date),
            status=definition.status,
        )
    if definition.start_local is None or definition.duration_seconds is None:
        raise ValueError("timed override is missing its local start or duration")
    return OverrideEvent(
        recurrence_key=definition.recurrence_key,
        start_local=datetime.strptime(definition.start_local, _LOCAL_FORMAT),
        start_tz=definition.start_tz,
        start_utc=(
            datetime.fromtimestamp(definition.start_utc, UTC)
            if definition.start_utc is not None
            else None
        ),
        duration_seconds=definition.duration_seconds,
        status=definition.status,
    )


def _priority_for(definition: EventDefinition) -> str:
    override = definition.xprops.get("hermes_calendar.priority")
    if isinstance(override, str) and override in {
        "digest",
        "normal",
        "high",
        "critical",
    }:
        return override
    if definition.transparency == "TRANSPARENT":
        return "digest"
    return "normal"


def _drafts_for(
    definition: EventDefinition,
    occurrence: Occurrence,
    reminder: ReminderDefinition,
    *,
    store_id: str,
    settings: NotificationSettings,
) -> list[InstanceDraft]:
    repeats = max(0, reminder.repeat_count)
    interval = reminder.repeat_interval_seconds or 0
    primaries = _primary_fire(definition, occurrence, reminder, settings)
    priority = _priority_for(definition)
    drafts: list[InstanceDraft] = []
    for index in range(repeats + 1):
        fire_at = primaries + index * interval
        offset = _materialized_offset(definition, occurrence, reminder, fire_at)
        recurrence_key = occurrence.recurrence_key
        if index:
            recurrence_key = f"{recurrence_key}#r{index}"
        drafts.append(
            InstanceDraft(
                store_id=store_id,
                event_id=definition.id,
                reminder_id=reminder.id,
                recurrence_key=recurrence_key,
                occurrence_start_utc=int(occurrence.start_utc.timestamp()),
                offset_seconds=offset,
                fire_at_utc=fire_at,
                priority=priority,
                channel_policy={
                    "action": reminder.action,
                    "related": reminder.related,
                },
                dedup_key=dedup_key(store_id, definition.id, reminder.id, recurrence_key),
            )
        )
    return drafts


def _materialized_offset(
    definition: EventDefinition,
    occurrence: Occurrence,
    reminder: ReminderDefinition,
    fire_at: int,
) -> int:
    if reminder.trigger_kind == "RELATIVE" and reminder.related == "START":
        return reminder.offset_seconds or 0
    return fire_at - int(occurrence.start_utc.timestamp())


def _primary_fire(
    definition: EventDefinition,
    occurrence: Occurrence,
    reminder: ReminderDefinition,
    settings: NotificationSettings,
) -> int:
    if reminder.trigger_kind == "ABSOLUTE":
        if reminder.absolute_utc is None:
            raise ValueError("absolute trigger is missing absolute_utc")
        return reminder.absolute_utc
    offset = reminder.offset_seconds or 0
    if not definition.is_all_day:
        base = occurrence.end_utc if reminder.related == "END" else occurrence.start_utc
        return int(base.timestamp()) + offset

    assert occurrence.start_date is not None
    assert occurrence.end_date is not None
    day = (
        occurrence.end_date - timedelta(days=1)
        if reminder.related == "END"
        else occurrence.start_date
    )
    zone = _zone(settings.timezone)
    local = datetime.combine(day, settings.all_day_fire_time).replace(tzinfo=zone, fold=0)
    return int(local.timestamp()) + offset


def _target_of(notifier: Notifier) -> str:
    """Return a non-secret channel label for the delivery log."""
    config = getattr(notifier, "config", None)
    target = getattr(config, "target", None)
    return str(target) if target else notifier.name


__all__ = ["ProfileMismatchError", "ReminderEngine"]
