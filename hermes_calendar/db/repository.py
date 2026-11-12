"""Repository: the only supported write path for events and calendars.

Every write runs in ``BEGIN IMMEDIATE`` and sets the singleton ``change_context``
as the first statement, so SQLite triggers attribute the change to the right
origin, actor, and batch (``core.md`` §7.3). Updates and deletes use
``events.rev`` as a compare-and-swap token, which is what makes several profile
processes writing the global store safe.

This M1 slice intentionally has no recurrence expansion, iTIP handling, FTS, or
sync apply path. ``list_events`` returns only non-recurring rows.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal, cast

from ..models import (
    Attendee,
    Calendar,
    Event,
    EventDraft,
    Reminder,
    Source,
    new_uuid7,
)
from .errors import (
    CalendarNotFoundError,
    EventNotFoundError,
    RevisionConflict,
    SourceNotFoundError,
)

Origin = Literal["local", "remote", "system"]


class Repository:
    """Event and calendar operations over one store connection.

    The connection must already be migrated. A repository does not open or close
    it; pass one connection per concurrent writer.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (callers must not write SQL through it)."""
        return self._connection

    # ------------------------------------------------------------------ events

    def create_event(
        self,
        draft: EventDraft,
        *,
        origin: Origin = "local",
        actor: str | None = None,
        batch_id: str | None = None,
    ) -> Event:
        """Insert an event aggregate and return the stored row.

        Identifiers are minted when omitted. ``source_id`` and ``scope`` come
        from the target calendar; in the global store the creating profile is
        recorded as ``owner_profile_id`` when the caller does not supply one.
        Child inserts bump ``rev`` via triggers, so the returned revision may be
        greater than one.
        """
        now = int(time.time())
        with self._write(origin, actor, batch_id):
            calendar = self._require_calendar(draft.calendar_id)
            event_id = draft.id or new_uuid7()
            owner = calendar.owner_profile_id
            if calendar.scope != "profile":
                owner = draft.owner_profile_id or actor
            duration = draft.duration_seconds
            if duration is None and not draft.is_all_day:
                duration = draft.end_utc - draft.start_utc
            values: dict[str, Any] = {
                "id": event_id,
                "calendar_id": calendar.id,
                "source_id": calendar.source_id,
                "scope": calendar.scope,
                "owner_profile_id": owner,
                "uid": draft.uid or f"{new_uuid7()}@hermes.local",
                "rev": 1,
                "created_utc": draft.created_utc if draft.created_utc is not None else now,
                "updated_utc": now,
                "deleted_utc": None,
            }
            values.update(_mutable_values(draft, duration=duration))
            self._insert("events", values)
            self._insert_children(event_id, draft.attendees, draft.reminders)
            return self._event_from_row(self._require_event_row(event_id))

    def get_event(self, event_id: str) -> Event | None:
        """Return an event aggregate, including tombstones, or ``None``."""
        row = self._connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return None if row is None else self._event_from_row(row)

    def update_event(
        self,
        event: Event,
        *,
        expected_rev: int,
        origin: Origin = "local",
        actor: str | None = None,
        batch_id: str | None = None,
    ) -> Event:
        """Replace mutable event fields with compare-and-swap on ``rev``.

        ``id``, ``calendar_id``, ``source_id``, ``scope``, ``owner_profile_id``
        and ``uid`` are immutable. A stale ``expected_rev`` raises
        :class:`RevisionConflict` and changes nothing.

        Attendees and reminders are replaced only when the supplied tuples differ
        from the stored aggregate, so ``dataclasses.replace(event, ...)`` on a
        value from :meth:`get_event` does not churn child rows.
        """
        now = int(time.time())
        with self._write(origin, actor, batch_id):
            current = self._event_from_row(self._require_event_row(event.id))
            _reject_immutable_changes(current, event)
            values = _mutable_values(event)
            values["updated_utc"] = now
            assignments = ", ".join(f"{name} = :{name}" for name in values)
            values["event_id"] = event.id
            values["expected_rev"] = expected_rev
            cursor = self._connection.execute(
                f"""
                UPDATE events
                   SET {assignments}, rev = rev + 1
                 WHERE id = :event_id AND rev = :expected_rev
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(event.id, expected_rev)
            if current.attendees != event.attendees or current.reminders != event.reminders:
                self._connection.execute(
                    "DELETE FROM event_attendees WHERE event_id = ?", (event.id,)
                )
                self._connection.execute(
                    "DELETE FROM event_reminders WHERE event_id = ?", (event.id,)
                )
                self._insert_children(event.id, event.attendees, event.reminders)
            return self._event_from_row(self._require_event_row(event.id))

    def delete_event(
        self,
        event_id: str,
        *,
        expected_rev: int | None = None,
        origin: Origin = "local",
        actor: str | None = None,
        batch_id: str | None = None,
    ) -> None:
        """Soft-delete an event, keeping the row and its ``etag`` for the pusher.

        Deleting an already-tombstoned event is a no-op. When ``expected_rev`` is
        supplied it must match the current row.
        """
        now = int(time.time())
        with self._write(origin, actor, batch_id):
            row = self._require_event_row(event_id)
            current = self._event_from_row(row)
            if current.deleted_utc is not None:
                return
            expected = current.rev if expected_rev is None else expected_rev
            cursor = self._connection.execute(
                """
                UPDATE events
                   SET deleted_utc = ?, rev = rev + 1, updated_utc = ?
                 WHERE id = ? AND rev = ? AND deleted_utc IS NULL
                """,
                (now, now, event_id, expected),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(event_id, expected)

    def list_events(
        self,
        calendar: str,
        start_utc: int,
        end_utc: int,
    ) -> list[Event]:
        """Return non-recurring, non-deleted events overlapping ``[start, end)``.

        All-day events participate through their UTC-midnight sort keys. Recurring
        masters and overrides are excluded in this M1 slice.
        """
        if start_utc >= end_utc:
            raise ValueError("start_utc must be earlier than end_utc")
        rows = self._connection.execute(
            """
            SELECT * FROM events
             WHERE calendar_id = ?
               AND deleted_utc IS NULL
               AND parent_id IS NULL
               AND rrule IS NULL
               AND start_utc < ?
               AND end_utc > ?
             ORDER BY start_utc, id
            """,
            (calendar, end_utc, start_utc),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    # --------------------------------------------------------------- calendars

    def create_calendar(
        self,
        name: str,
        *,
        calendar_id: str | None = None,
        source_id: str | None = None,
        description: str | None = None,
        color: str | None = None,
        default_tz: str = "UTC",
        read_only: bool = False,
        remote_id: str | None = None,
        origin: Origin = "local",
        actor: str | None = None,
        batch_id: str | None = None,
    ) -> Calendar:
        """Create a calendar in this store's first local source by default."""
        now = int(time.time())
        with self._write(origin, actor, batch_id):
            source = self._require_source(source_id) if source_id else self._local_source()
            values: dict[str, Any] = {
                "id": calendar_id or f"cal_{new_uuid7()}",
                "source_id": source.id,
                "scope": source.scope,
                "owner_profile_id": source.owner_profile_id,
                "remote_id": remote_id,
                "name": name,
                "description": description,
                "color": color,
                "default_tz": default_tz,
                "read_only": int(read_only),
                "sync_token": None,
                "ctag": None,
                "created_utc": now,
                "updated_utc": now,
                "deleted_utc": None,
            }
            self._insert("calendars", values)
            return self._calendar_from_row(self._require_calendar_row(str(values["id"])))

    def list_calendars(self) -> list[Calendar]:
        """Return non-deleted calendars ordered by name then id."""
        rows = self._connection.execute(
            """
            SELECT * FROM calendars
             WHERE deleted_utc IS NULL
             ORDER BY name, id
            """
        ).fetchall()
        return [self._calendar_from_row(row) for row in rows]

    # ---------------------------------------------------------------- internals

    @contextmanager
    def _write(
        self,
        origin: Origin,
        actor: str | None,
        batch_id: str | None,
    ) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                UPDATE change_context
                   SET origin = ?, actor = ?, batch_id = ?, suppress = 0
                 WHERE id = 1
                """,
                (origin, actor, batch_id),
            )
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def _insert(self, table: str, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        self._connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)

    def _insert_children(
        self,
        event_id: str,
        attendees: Sequence[Attendee],
        reminders: Sequence[Reminder],
    ) -> None:
        for ordinal, attendee in enumerate(attendees):
            self._connection.execute(
                """
                INSERT INTO event_attendees
                  (event_id, ordinal, address, email, name, role, partstat, rsvp,
                   cutype, delegated_to, delegated_from, sent_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    ordinal,
                    attendee.address,
                    attendee.email,
                    attendee.name,
                    attendee.role,
                    attendee.partstat,
                    int(attendee.rsvp),
                    attendee.cutype,
                    attendee.delegated_to,
                    attendee.delegated_from,
                    attendee.sent_by,
                ),
            )
        for reminder in reminders:
            self._connection.execute(
                """
                INSERT INTO event_reminders
                  (id, event_id, action, related, trigger_kind, offset_seconds,
                   absolute_utc, repeat_count, repeat_interval_seconds, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.id or new_uuid7(),
                    event_id,
                    reminder.action,
                    reminder.related,
                    reminder.trigger_kind,
                    reminder.offset_seconds,
                    reminder.absolute_utc,
                    reminder.repeat_count,
                    reminder.repeat_interval_seconds,
                    reminder.description,
                ),
            )

    def _require_event_row(self, event_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise EventNotFoundError(event_id)
        return cast(sqlite3.Row, row)

    def _require_calendar_row(self, calendar_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM calendars WHERE id = ?", (calendar_id,)
        ).fetchone()
        if row is None:
            raise CalendarNotFoundError(calendar_id)
        return cast(sqlite3.Row, row)

    def _require_calendar(self, calendar_id: str) -> Calendar:
        calendar = self._calendar_from_row(self._require_calendar_row(calendar_id))
        if calendar.deleted_utc is not None:
            raise CalendarNotFoundError(calendar_id)
        return calendar

    def _require_source(self, source_id: str) -> Source:
        row = self._connection.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise SourceNotFoundError(source_id)
        source = self._source_from_row(row)
        if source.deleted_utc is not None:
            raise SourceNotFoundError(source_id)
        return source

    def _local_source(self) -> Source:
        row = self._connection.execute(
            """
            SELECT * FROM sources
             WHERE kind = 'local' AND deleted_utc IS NULL
             ORDER BY id
             LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise SourceNotFoundError("local")
        return self._source_from_row(row)

    def _event_from_row(self, row: sqlite3.Row) -> Event:
        attendees, reminders = self._children(str(row["id"]))
        return Event(
            id=str(row["id"]),
            calendar_id=str(row["calendar_id"]),
            source_id=str(row["source_id"]),
            scope=str(row["scope"]),
            owner_profile_id=_optional_str(row["owner_profile_id"]),
            uid=str(row["uid"]),
            remote_href=_optional_str(row["remote_href"]),
            etag=_optional_str(row["etag"]),
            rev=int(row["rev"]),
            sequence=int(row["sequence"]),
            summary=str(row["summary"]),
            description=_optional_str(row["description"]),
            location=_optional_str(row["location"]),
            url=_optional_str(row["url"]),
            start_local=_optional_str(row["start_local"]),
            start_tz=_optional_str(row["start_tz"]),
            start_utc=int(row["start_utc"]),
            end_local=_optional_str(row["end_local"]),
            end_tz=_optional_str(row["end_tz"]),
            end_utc=int(row["end_utc"]),
            duration_seconds=_optional_int(row["duration_seconds"]),
            start_date=_optional_str(row["start_date"]),
            end_date=_optional_str(row["end_date"]),
            is_all_day=bool(row["is_all_day"]),
            is_floating=bool(row["is_floating"]),
            rrule=_optional_str(row["rrule"]),
            rrule_unsupported=_load_strings(row["rrule_unsupported_json"]),
            rdate=_load_strings(row["rdate_json"]),
            exdate=_load_strings(row["exdate_json"]),
            parent_id=_optional_str(row["parent_id"]),
            recurrence_key=_optional_str(row["recurrence_key"]),
            recurrence_id_raw=_optional_str(row["recurrence_id_raw"]),
            recurrence_range=_optional_str(row["recurrence_range"]),
            status=str(row["status"]),
            transparency=str(row["transparency"]),
            classification=str(row["classification"]),
            organizer_email=_optional_str(row["organizer_email"]),
            organizer_name=_optional_str(row["organizer_name"]),
            categories=_load_strings(row["categories_json"]) or (),
            xprops=_load_object(str(row["xprops_json"])),
            created_utc=_optional_int(row["created_utc"]),
            updated_utc=int(row["updated_utc"]),
            dtstamp_utc=_optional_int(row["dtstamp_utc"]),
            last_modified_utc=_optional_int(row["last_modified_utc"]),
            deleted_utc=_optional_int(row["deleted_utc"]),
            attendees=attendees,
            reminders=reminders,
        )

    def _children(self, event_id: str) -> tuple[tuple[Attendee, ...], tuple[Reminder, ...]]:
        attendee_rows = self._connection.execute(
            """
            SELECT * FROM event_attendees
             WHERE event_id = ?
             ORDER BY ordinal
            """,
            (event_id,),
        ).fetchall()
        reminder_rows = self._connection.execute(
            """
            SELECT * FROM event_reminders
             WHERE event_id = ?
             ORDER BY rowid
            """,
            (event_id,),
        ).fetchall()
        attendees = tuple(_attendee_from_row(row) for row in attendee_rows)
        reminders = tuple(_reminder_from_row(row) for row in reminder_rows)
        return attendees, reminders

    def _calendar_from_row(self, row: sqlite3.Row) -> Calendar:
        return Calendar(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            scope=str(row["scope"]),
            owner_profile_id=_optional_str(row["owner_profile_id"]),
            remote_id=_optional_str(row["remote_id"]),
            name=str(row["name"]),
            description=_optional_str(row["description"]),
            color=_optional_str(row["color"]),
            default_tz=str(row["default_tz"]),
            read_only=bool(row["read_only"]),
            sync_token=_optional_str(row["sync_token"]),
            ctag=_optional_str(row["ctag"]),
            created_utc=int(row["created_utc"]),
            updated_utc=int(row["updated_utc"]),
            deleted_utc=_optional_int(row["deleted_utc"]),
        )

    def _source_from_row(self, row: sqlite3.Row) -> Source:
        return Source(
            id=str(row["id"]),
            scope=str(row["scope"]),
            owner_profile_id=_optional_str(row["owner_profile_id"]),
            kind=str(row["kind"]),
            display_name=str(row["display_name"]),
            config=_load_object(str(row["config_json"])),
            capabilities=_load_object(str(row["capabilities_json"])),
            created_utc=int(row["created_utc"]),
            updated_utc=int(row["updated_utc"]),
            deleted_utc=_optional_int(row["deleted_utc"]),
        )


def _mutable_values(source: EventDraft | Event, *, duration: int | None = None) -> dict[str, Any]:
    """Map model values to SQLite columns for insert/update."""
    duration_seconds = source.duration_seconds if duration is None else duration
    values: dict[str, Any] = {
        "remote_href": source.remote_href,
        "etag": source.etag,
        "sequence": source.sequence,
        "summary": source.summary,
        "description": source.description,
        "location": source.location,
        "url": source.url,
        "start_local": source.start_local,
        "start_tz": source.start_tz,
        "start_utc": source.start_utc,
        "end_local": source.end_local,
        "end_tz": source.end_tz,
        "end_utc": source.end_utc,
        "duration_seconds": duration_seconds,
        "start_date": source.start_date,
        "end_date": source.end_date,
        "is_all_day": int(source.is_all_day),
        "is_floating": int(source.is_floating),
        "rrule": source.rrule,
        "rrule_unsupported_json": _dump_strings(source.rrule_unsupported),
        "rdate_json": _dump_strings(source.rdate),
        "exdate_json": _dump_strings(source.exdate),
        "parent_id": source.parent_id,
        "recurrence_key": source.recurrence_key,
        "recurrence_id_raw": source.recurrence_id_raw,
        "recurrence_range": source.recurrence_range,
        "status": source.status,
        "transparency": source.transparency,
        "classification": source.classification,
        "organizer_email": source.organizer_email,
        "organizer_name": source.organizer_name,
        "categories_json": _dump_strings(source.categories) or "[]",
        "xprops_json": json.dumps(source.xprops, sort_keys=True),
        "dtstamp_utc": source.dtstamp_utc,
        "last_modified_utc": source.last_modified_utc,
    }
    if isinstance(source, Event):
        values["deleted_utc"] = source.deleted_utc
    return values


def _reject_immutable_changes(current: Event, proposed: Event) -> None:
    fields = (
        "calendar_id",
        "source_id",
        "scope",
        "owner_profile_id",
        "uid",
    )
    for field in fields:
        if getattr(current, field) != getattr(proposed, field):  # noqa: B009
            raise ValueError(f"event field {field} is immutable")


def _dump_strings(values: tuple[str, ...] | None) -> str | None:
    return None if values is None else json.dumps(list(values))


def _load_strings(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    loaded: Any = json.loads(str(value))
    return tuple(str(item) for item in loaded)


def _load_object(value: str) -> dict[str, Any]:
    loaded: Any = json.loads(value)
    return {str(key): item for key, item in loaded.items()}


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))


def _attendee_from_row(row: sqlite3.Row) -> Attendee:
    return Attendee(
        address=str(row["address"]),
        email=_optional_str(row["email"]),
        name=_optional_str(row["name"]),
        role=str(row["role"]),
        partstat=str(row["partstat"]),
        rsvp=bool(row["rsvp"]),
        cutype=_optional_str(row["cutype"]),
        delegated_to=_optional_str(row["delegated_to"]),
        delegated_from=_optional_str(row["delegated_from"]),
        sent_by=_optional_str(row["sent_by"]),
    )


def _reminder_from_row(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=str(row["id"]),
        action=str(row["action"]),
        related=str(row["related"]),
        trigger_kind=str(row["trigger_kind"]),
        offset_seconds=_optional_int(row["offset_seconds"]),
        absolute_utc=_optional_int(row["absolute_utc"]),
        repeat_count=int(row["repeat_count"]),
        repeat_interval_seconds=_optional_int(row["repeat_interval_seconds"]),
        description=_optional_str(row["description"]),
    )
