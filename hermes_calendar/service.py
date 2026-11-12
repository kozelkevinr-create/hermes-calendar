"""Calendar use-cases shared by the Hermes tools and future dashboard API.

The service is the one place that owns store selection (``profile`` or
``global``), argument validation, timezone resolution, recurrence expansion,
and iCalendar import/export. :mod:`hermes_calendar.hermes.tools` is only a JSON
adapter over these methods, so a dashboard route can reuse the exact same
behavior without duplicating policy.

Times follow ``docs/design/core.md``: inputs are ISO 8601 datetime strings. A
value with ``Z`` or an offset is absolute; a value without an offset is
interpreted in the selected IANA zone (the ``timezone`` argument, else the
profile's configured zone, else UTC). Timed rows store the UTC instant, the
local wall clock, and the IANA zone. Recurrence expands in local wall time so a
09:00 meeting stays at 09:00 across a DST change.

Methods return JSON-shaped dictionaries. They raise :class:`ServiceError`
subclasses for model-visible failures and let store errors propagate; the tool
adapter maps both onto the plugin's ``{"ok": false, "code": ...}`` envelope.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import (
    CalendarNotFoundError,
    EventNotFoundError,
    Repository,
    RevisionConflict,
    open_global_store,
    open_profile_store,
    profile_name,
)
from .ics import MAX_ICS_BYTES, IcsLimitError, parse_ics, serialize_events
from .models import Calendar, Event, EventDraft
from .plugin_storage import profile_dir
from .recurrence import (
    ExpansionLimitExceeded,
    MasterEvent,
    Occurrence,
    OverrideEvent,
    UnsupportedRecurrence,
    all_day_recurrence_key,
    expand,
    timed_recurrence_key,
)
from .recurrence.rule import P0_RULE_PARTS, SUPPORTED_FREQUENCIES, parse_rule

__all__ = [
    "DEFAULT_SCOPE",
    "MAX_TEXT",
    "CalendarService",
    "Conflict",
    "InvalidArgument",
    "Scope",
    "ServiceError",
    "UnsupportedRecurrenceError",
]

PLUGIN_NAME = "hermes-calendar"
DEFAULT_SCOPE = "profile"
MAX_TEXT = 20_000
_SCOPES: tuple[Scope, ...] = ("profile", "global")
_SERIES_SCOPES = frozenset({"this", "all"})
_LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"
_TIMEZONE_CONFIG_RE = re.compile(
    r"^timezone[ \t]*:[ \t]*(?P<value>[^#\n]*?)[ \t]*(?:#.*)?$", re.MULTILINE
)

Scope = Literal["profile", "global"]
StoreOpener = Callable[[Scope], AbstractContextManager[Repository]]
_ChangeMap = Mapping[str, Any]

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPACT_DATE_RE = re.compile(r"^\d{8}$")
_DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_COMPACT_DATE_TIME_RE = re.compile(r"^\d{8}T\d{6}Z?$")


class ServiceError(Exception):
    """Base class for failures that map to a structured tool error."""

    code = "service_error"


class InvalidArgument(ServiceError):
    """The caller supplied an argument that cannot be used."""

    code = "invalid_argument"


class Conflict(ServiceError):
    """The request conflicts with existing state but is otherwise valid."""

    code = "conflict"


class UnsupportedRecurrenceError(ServiceError):
    """An RRULE uses a part outside the supported RFC 5545 subset."""

    code = "unsupported_recurrence"


@contextmanager
def _default_store(scope: Scope) -> Iterator[Repository]:
    """Open exactly one store for the duration of a service call."""
    connection = open_profile_store() if scope == "profile" else open_global_store()
    try:
        yield Repository(connection)
    finally:
        connection.close()


class CalendarService:
    """Calendar use-cases over one profile or global store.

    ``open_store`` exists so callers (currently the tool adapter's tests) can
    observe or replace store opening. Production code uses the default, which
    opens the profile or global database for the requested scope.
    """

    def __init__(self, open_store: StoreOpener | None = None) -> None:
        self._open_store = _default_store if open_store is None else open_store

    @contextmanager
    def _repository(self, scope: Scope) -> Iterator[Repository]:
        with self._open_store(scope) as repository:
            yield repository

    # ---------------------------------------------------------------- calendars

    def list_calendars(self, *, scope: Any = DEFAULT_SCOPE) -> dict[str, Any]:
        """List calendars in the selected store."""
        resolved = _scope(scope)
        with self._repository(resolved) as repository:
            calendars = repository.list_calendars()
        return {
            "scope": resolved,
            "count": len(calendars),
            "calendars": [_calendar_payload(calendar) for calendar in calendars],
        }

    def create_calendar(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        name: Any = None,
        description: Any = None,
        color: Any = None,
        default_tz: Any = None,
        read_only: Any = None,
    ) -> dict[str, Any]:
        """Create a calendar in the selected store."""
        resolved = _scope(scope)
        calendar_name = _require_str(name, "name")
        calendar_description = _optional_str(description, "description")
        calendar_color = _optional_str(color, "color")
        calendar_read_only = _optional_bool(read_only, "read_only", default=False)
        provided_tz = _validated_zone_arg(default_tz, "default_tz")
        zone_name = _default_timezone_name() if provided_tz is None else provided_tz
        with self._repository(resolved) as repository:
            if resolved == "global" and repository.list_calendars():
                raise Conflict(
                    "the global store already has its single shared calendar; "
                    "create additional calendars in a profile store"
                )
            calendar = repository.create_calendar(
                calendar_name,
                description=calendar_description,
                color=calendar_color,
                default_tz=zone_name,
                read_only=calendar_read_only,
            )
        return {"scope": resolved, "calendar": _calendar_payload(calendar)}

    # ------------------------------------------------------------------- events

    def create_event(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        calendar_id: Any = None,
        summary: Any = None,
        start: Any = None,
        end: Any = None,
        timezone: Any = None,
        description: Any = None,
        location: Any = None,
        url: Any = None,
        rrule: Any = None,
        rdates: Any = None,
        exdates: Any = None,
    ) -> dict[str, Any]:
        """Create a timed event, optionally recurring, in an existing calendar."""
        resolved = _scope(scope)
        target = _require_str(calendar_id, "calendar_id")
        title = _require_str(summary, "summary")
        zone = _zone(_timezone_name(timezone))
        start_time = _parse_time(start, "start", zone)
        end_time = _parse_time(end, "end", zone)
        _validate_span(start_time, end_time)
        normalized_rule = _normalize_rrule(rrule)
        draft = EventDraft(
            calendar_id=target,
            summary=title,
            description=_optional_str(description, "description"),
            location=_optional_str(location, "location"),
            url=_optional_str(url, "url"),
            start_utc=start_time.utc,
            end_utc=end_time.utc,
            start_local=start_time.local,
            start_tz=start_time.tz,
            end_local=end_time.local,
            end_tz=end_time.tz,
            duration_seconds=end_time.utc - start_time.utc,
            rrule=normalized_rule,
            rdate=_recurrence_values(rdates, "rdates", all_day=False),
            exdate=_recurrence_values(exdates, "exdates", all_day=False),
        )
        with self._repository(resolved) as repository:
            event = repository.create_event(draft, actor=profile_name())
        return {"scope": resolved, "event": _event_payload(event)}

    def get_event(self, *, scope: Any = DEFAULT_SCOPE, event_id: Any = None) -> dict[str, Any]:
        """Return one non-deleted event by ID."""
        resolved = _scope(scope)
        target = _require_str(event_id, "event_id")
        with self._repository(resolved) as repository:
            event = repository.get_event(target)
        if event is None or event.deleted_utc is not None:
            raise EventNotFoundError(target)
        return {"scope": resolved, "event": _event_payload(event)}

    def update_event(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        event_id: Any = None,
        expected_rev: Any = None,
        changes: _ChangeMap | None = None,
        occurrence_start: Any = None,
        series_scope: Any = None,
    ) -> dict[str, Any]:
        """Update event fields, or a single occurrence of a recurring series.

        ``expected_rev`` always guards the master row, including when editing
        one occurrence; this keeps one compare-and-swap token for the whole
        series aggregate.
        """
        resolved = _scope(scope)
        target = _require_str(event_id, "event_id")
        revision = _require_int(expected_rev, "expected_rev", minimum=1)
        requested = {} if changes is None else dict(changes)
        mode, occurrence_value = _series_mode(occurrence_start, series_scope)
        with self._repository(resolved) as repository:
            current = _require_event(repository, target)
            master = _master_of(repository, current)
            if master.rev != revision:
                raise RevisionConflict(master.id, revision)
            if mode == "this":
                event = self._update_occurrence(
                    repository,
                    master,
                    occurrence_value,
                    requested,
                    scope=resolved,
                )
            else:
                event = self._update_series(repository, master, requested)
        return {"scope": resolved, "event": _event_payload(event)}

    def delete_event(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        event_id: Any = None,
        expected_rev: Any = None,
        occurrence_start: Any = None,
        series_scope: Any = None,
    ) -> dict[str, Any]:
        """Soft-delete one event, one occurrence, or a whole series."""
        resolved = _scope(scope)
        target = _require_str(event_id, "event_id")
        revision = _optional_int(expected_rev, "expected_rev", minimum=1)
        mode, occurrence_value = _series_mode(occurrence_start, series_scope)
        with self._repository(resolved) as repository:
            current = repository.get_event(target)
            if current is None:
                raise EventNotFoundError(target)
            if current.deleted_utc is not None and mode == "all":
                return {
                    "scope": resolved,
                    "event_id": target,
                    "deleted": True,
                    "already_deleted": True,
                }
            master = _master_of(repository, current)
            if revision is not None and master.rev != revision:
                raise RevisionConflict(master.id, revision)
            if mode == "this":
                override = self._delete_occurrence(
                    repository,
                    master,
                    occurrence_value,
                    scope=resolved,
                )
                return {
                    "scope": resolved,
                    "event_id": target,
                    "deleted": True,
                    "already_deleted": False,
                    "occurrence_start": occurrence_value,
                    "recurrence_key": override.recurrence_key,
                    "override_event_id": override.id,
                }
            overrides = repository.list_overrides(master.id)
            for override in overrides:
                if override.deleted_utc is None:
                    repository.delete_event(
                        override.id,
                        expected_rev=None,
                        actor=profile_name(),
                    )
            repository.delete_event(
                master.id,
                expected_rev=revision,
                actor=profile_name(),
            )
            result: dict[str, Any] = {
                "scope": resolved,
                "event_id": target,
                "deleted": True,
                "already_deleted": False,
            }
            removed = len([item for item in overrides if item.deleted_utc is None])
            if removed:
                result["deleted_overrides"] = removed
            return result

    def list_events(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        start: Any = None,
        end: Any = None,
        timezone: Any = None,
        calendar_id: Any = None,
    ) -> dict[str, Any]:
        """List non-recurring events and expanded recurring occurrences.

        Each item carries ``occurrence_start``/``occurrence_end``, ``master_id``
        and ``is_override`` so a caller can tell one instance from another.
        Non-recurring rows also get those keys, with the row's own start/end.
        """
        resolved = _scope(scope)
        zone = _zone(_timezone_name(timezone))
        start_time = _parse_time(start, "start", zone)
        end_time = _parse_time(end, "end", zone)
        _validate_span(start_time, end_time)
        requested_calendar = _optional_str(calendar_id, "calendar_id")
        with self._repository(resolved) as repository:
            calendars = repository.list_calendars()
            if requested_calendar is None:
                calendar_ids = [calendar.id for calendar in calendars]
            else:
                if requested_calendar not in {calendar.id for calendar in calendars}:
                    raise CalendarNotFoundError(requested_calendar)
                calendar_ids = [requested_calendar]
            payloads: list[dict[str, Any]] = []
            warnings: list[str] = []
            expanded = False
            for target in calendar_ids:
                rows = _list_calendar_events(
                    repository,
                    target,
                    start_time.utc,
                    end_time.utc,
                    warnings,
                )
                payloads.extend(rows)
                expanded = expanded or _has_recurring_master(repository, target)
        payloads.sort(key=_occurrence_sort_key)
        result: dict[str, Any] = {
            "scope": resolved,
            "range": {"start": start_time.utc, "end": end_time.utc, "timezone": zone.key},
            "count": len(payloads),
            "recurrence_expanded": expanded,
            "events": payloads,
        }
        if warnings:
            result["recurrence_warnings"] = warnings
        return result

    # --------------------------------------------------------------------- ICS

    def import_ics(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        calendar_id: Any = None,
        text: Any = None,
    ) -> dict[str, Any]:
        """Import iCalendar text into a calendar, idempotently by ``UID``.

        Re-importing the same document updates the existing master and override
        rows instead of inserting duplicates. Malformed input and parser limits
        are reported as warnings with zero counts; this method never raises for
        bad calendar text. It never fetches a URL.
        """
        resolved = _scope(scope)
        target = _require_str(calendar_id, "calendar_id")
        if not isinstance(text, str):
            raise InvalidArgument("text is required and must be an iCalendar string")
        with self._repository(resolved) as repository:
            _require_calendar(repository, target)
            try:
                parsed = parse_ics(text, max_bytes=MAX_ICS_BYTES)
            except IcsLimitError as exc:
                return {
                    "scope": resolved,
                    "calendar_id": target,
                    "imported": 0,
                    "updated": 0,
                    "skipped": 0,
                    "warnings": [str(exc)],
                }
            imported = 0
            updated = 0
            skipped = 0
            warnings = list(parsed.warnings)
            masters: dict[str, Event] = {}
            overrides: list[EventDraft] = []
            for draft in parsed.events:
                if draft.recurrence_key is None:
                    if draft.uid in masters:
                        warnings.append(
                            f"{draft.uid}: duplicate master UID in document; extra master skipped"
                        )
                        skipped += 1
                        continue
                    prepared, draft_warnings = _prepare_master(draft, target, warnings)
                    warnings = draft_warnings
                    existing = repository.get_event_by_uid(target, prepared.uid or "")
                    if existing is None:
                        series_master = repository.create_event(prepared, actor=profile_name())
                        imported += 1
                    else:
                        series_master = repository.update_event(
                            _apply_draft(existing, prepared),
                            expected_rev=existing.rev,
                            actor=profile_name(),
                        )
                        updated += 1
                    masters[str(prepared.uid)] = series_master
                else:
                    overrides.append(draft)

            for draft in overrides:
                uid = draft.uid or ""
                master: Event | None = masters.get(uid)
                if master is None:
                    existing_master = repository.get_event_by_uid(target, uid)
                    if existing_master is None:
                        warnings.append(f"{uid}: override without a master in this import; skipped")
                        skipped += 1
                        continue
                    master = existing_master
                    masters[uid] = master
                prepared, draft_warnings = _prepare_override(draft, master, warnings)
                warnings = draft_warnings
                existing_override = repository.get_override(
                    master.id, prepared.recurrence_key or ""
                )
                if existing_override is None:
                    repository.create_event(prepared, actor=profile_name())
                    imported += 1
                else:
                    repository.update_event(
                        _apply_draft(existing_override, prepared),
                        expected_rev=existing_override.rev,
                        actor=profile_name(),
                    )
                    updated += 1
        result: dict[str, Any] = {
            "scope": resolved,
            "calendar_id": target,
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "warnings": warnings,
        }
        return result

    def export_ics(
        self,
        *,
        scope: Any = DEFAULT_SCOPE,
        calendar_id: Any = None,
        start: Any = None,
        end: Any = None,
        timezone: Any = None,
    ) -> dict[str, Any]:
        """Export one calendar as deterministic iCalendar text.

        Master rows are exported with their ``RRULE``/``RDATE``/``EXDATE`` and
        overrides, not as expanded instances, so a re-import keeps the series
        identity. Non-recurring rows are included when they overlap the window.
        """
        resolved = _scope(scope)
        target = _require_str(calendar_id, "calendar_id")
        zone = _zone(_timezone_name(timezone))
        start_time = _parse_time(start, "start", zone)
        end_time = _parse_time(end, "end", zone)
        _validate_span(start_time, end_time)
        with self._repository(resolved) as repository:
            _require_calendar(repository, target)
            selected: dict[str, Event] = {}
            for event in repository.list_events(target, start_time.utc, end_time.utc):
                selected[event.id] = event
            for master in repository.list_masters(target):
                overrides = [
                    item
                    for item in repository.list_overrides(master.id)
                    if item.deleted_utc is None
                ]
                if _series_overlaps(
                    master,
                    overrides,
                    start_time.utc,
                    end_time.utc,
                ):
                    selected[master.id] = master
                    for override in overrides:
                        selected[override.id] = override
        events = sorted(selected.values(), key=lambda item: (item.start_utc, item.id))
        text = serialize_events(_event_to_draft(event) for event in events)
        return {
            "scope": resolved,
            "calendar_id": target,
            "range": {"start": start_time.utc, "end": end_time.utc, "timezone": zone.key},
            "count": len(events),
            "ics": text,
        }

    # --------------------------------------------------------------- occurrence

    def _update_series(
        self,
        repository: Repository,
        master: Event,
        requested: _ChangeMap,
    ) -> Event:
        """Apply whole-series changes to the master row."""
        changes = _series_changes(requested, master)
        if not changes:
            raise InvalidArgument("provide at least one field to update")
        return repository.update_event(
            replace(master, **changes),
            expected_rev=master.rev,
            actor=profile_name(),
        )

    def _update_occurrence(
        self,
        repository: Repository,
        master: Event,
        occurrence_raw: Any,
        requested: _ChangeMap,
        *,
        scope: Scope,
    ) -> Event:
        """Insert or update an override for one occurrence."""
        del scope
        original = _occurrence_for(repository, master, occurrence_raw)
        for forbidden in ("rrule", "rdates", "exdates", "occurrence_start", "series_scope"):
            if forbidden in requested:
                raise InvalidArgument(
                    f"{forbidden} cannot be changed for a single occurrence; use series_scope 'all'"
                )
        existing = repository.get_override(master.id, original.recurrence_key)
        base = _occurrence_base(master, original, existing)
        changes = _occurrence_changes(requested, base)
        if not changes:
            raise InvalidArgument("provide at least one field to update")
        draft = _draft_from_event(base, overrides=changes)
        draft = replace(
            draft,
            uid=master.uid,
            parent_id=master.id,
            recurrence_key=original.recurrence_key,
            recurrence_id_raw=_recurrence_id_raw(original, master),
            rrule=None,
            rrule_unsupported=None,
            rdate=None,
            exdate=None,
        )
        if existing is None:
            return repository.create_event(draft, actor=profile_name())
        return repository.update_event(
            _apply_draft(existing, draft),
            expected_rev=existing.rev,
            actor=profile_name(),
        )

    def _delete_occurrence(
        self,
        repository: Repository,
        master: Event,
        occurrence_raw: Any,
        *,
        scope: Scope,
    ) -> Event:
        """Cancel one occurrence with an override row (``core.md`` §6.5)."""
        del scope
        original = _occurrence_for(repository, master, occurrence_raw)
        existing = repository.get_override(master.id, original.recurrence_key)
        base = _occurrence_base(master, original, existing)
        draft = _draft_from_event(base, overrides={"status": "CANCELLED"})
        draft = replace(
            draft,
            uid=master.uid,
            parent_id=master.id,
            recurrence_key=original.recurrence_key,
            recurrence_id_raw=_recurrence_id_raw(original, master),
            rrule=None,
            rrule_unsupported=None,
            rdate=None,
            exdate=None,
            status="CANCELLED",
        )
        if existing is None:
            return repository.create_event(draft, actor=profile_name())
        if existing.status == "CANCELLED" and existing.deleted_utc is None:
            return existing
        return repository.update_event(
            _apply_draft(existing, draft),
            expected_rev=existing.rev,
            actor=profile_name(),
        )


# ------------------------------------------------------------------ validation


def _scope(value: Any) -> Scope:
    if value not in _SCOPES:
        raise InvalidArgument(
            f"scope must be one of {', '.join(repr(item) for item in _SCOPES)}; got {value!r}"
        )
    return cast(Scope, value)


def _series_mode(occurrence_start: Any, series_scope: Any) -> tuple[Literal["this", "all"], Any]:
    if series_scope is None:
        mode: Literal["this", "all"] = "this" if occurrence_start is not None else "all"
    elif series_scope in _SERIES_SCOPES:
        mode = cast(Literal["this", "all"], series_scope)
    else:
        raise InvalidArgument("series_scope must be 'this' or 'all'")
    if mode == "this" and occurrence_start is None:
        raise InvalidArgument("occurrence_start is required when series_scope is 'this'")
    return mode, occurrence_start


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(f"{name} is required and must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise InvalidArgument(f"{name} must be at most {MAX_TEXT} characters")
    return value.strip()


def _optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidArgument(f"{name} must be a string or null")
    if len(value) > MAX_TEXT:
        raise InvalidArgument(f"{name} must be at most {MAX_TEXT} characters")
    return value


def _optional_bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidArgument(f"{name} must be a boolean")
    return value


def _require_int(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(f"{name} is required and must be an integer")
    if value < minimum:
        raise InvalidArgument(f"{name} must be at least {minimum}")
    return value


def _optional_int(value: Any, name: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _require_int(value, name, minimum=minimum)


def _zone(name: str, *, field: str = "timezone") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidArgument(f"{field} must be a valid IANA zone name; got {name!r}") from exc


def _profile_home() -> Path:
    return profile_dir(PLUGIN_NAME).parent.parent


def _configured_timezone_name() -> str | None:
    env_value = os.environ.get("HERMES_TIMEZONE", "").strip()
    if env_value:
        return env_value
    try:
        text = (_profile_home() / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _TIMEZONE_CONFIG_RE.search(text)
    if match is None:
        return None
    value = match.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value or None


def _default_timezone_name() -> str:
    name = _configured_timezone_name()
    if name is None:
        return "UTC"
    try:
        _zone(name)
    except InvalidArgument:
        return "UTC"
    return name


def _validated_zone_arg(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(f"{name} must be a non-empty IANA zone name string")
    zone_name = value.strip()
    _zone(zone_name, field=name)
    return zone_name


def _timezone_name(value: Any) -> str:
    provided = _validated_zone_arg(value, "timezone")
    return _default_timezone_name() if provided is None else provided


@dataclass(frozen=True)
class _TimeValue:
    utc: int
    local: str
    tz: str


def _parse_time(value: Any, field: str, zone: ZoneInfo) -> _TimeValue:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(
            f"{field} is required and must be an ISO 8601 datetime string, "
            "for example 2026-03-02T09:00:00-05:00"
        )
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidArgument(
            f"{field} must be an ISO 8601 datetime, for example "
            "2026-03-02T09:00:00-05:00 or 2026-03-02T14:00:00Z; "
            f"got {value!r}"
        ) from exc
    parsed = parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
    return _TimeValue(
        utc=int(parsed.timestamp()),
        local=parsed.strftime(_LOCAL_FORMAT),
        tz=zone.key,
    )


def _time_from_utc(epoch: int, zone: ZoneInfo) -> _TimeValue:
    local = datetime.fromtimestamp(epoch, zone)
    return _TimeValue(utc=epoch, local=local.strftime(_LOCAL_FORMAT), tz=zone.key)


def _validate_span(start: _TimeValue, end: _TimeValue) -> None:
    if end.utc <= start.utc:
        raise InvalidArgument("end must be later than start")


def _rfc3339(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_payload(
    epoch: int, local: str | None, tz: str | None, date_value: str | None
) -> dict[str, Any]:
    return {"utc": _rfc3339(epoch), "local": local, "tz": tz, "date": date_value}


def _calendar_payload(calendar: Calendar) -> dict[str, Any]:
    return {
        "id": calendar.id,
        "source_id": calendar.source_id,
        "scope": calendar.scope,
        "owner_profile_id": calendar.owner_profile_id,
        "remote_id": calendar.remote_id,
        "name": calendar.name,
        "description": calendar.description,
        "color": calendar.color,
        "default_tz": calendar.default_tz,
        "read_only": calendar.read_only,
        "created_utc": calendar.created_utc,
        "updated_utc": calendar.updated_utc,
    }


def _event_payload(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "calendar_id": event.calendar_id,
        "scope": event.scope,
        "owner_profile_id": event.owner_profile_id,
        "uid": event.uid,
        "rev": event.rev,
        "sequence": event.sequence,
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "url": event.url,
        "start": _time_payload(
            event.start_utc, event.start_local, event.start_tz, event.start_date
        ),
        "end": _time_payload(event.end_utc, event.end_local, event.end_tz, event.end_date),
        "start_utc": event.start_utc,
        "end_utc": event.end_utc,
        "duration_seconds": event.duration_seconds,
        "all_day": event.is_all_day,
        "floating": event.is_floating,
        "rrule": event.rrule,
        "rrule_unsupported": None
        if event.rrule_unsupported is None
        else list(event.rrule_unsupported),
        "rdate": None if event.rdate is None else list(event.rdate),
        "exdate": None if event.exdate is None else list(event.exdate),
        "parent_id": event.parent_id,
        "recurrence_key": event.recurrence_key,
        "recurrence_id_raw": event.recurrence_id_raw,
        "recurrence_range": event.recurrence_range,
        "status": event.status,
        "transparency": event.transparency,
        "classification": event.classification,
        "categories": list(event.categories),
        "created_utc": event.created_utc,
        "updated_utc": event.updated_utc,
        "deleted_utc": event.deleted_utc,
    }


def _occurrence_sort_key(payload: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(cast(int, payload["occurrence_start_utc"])),
        str(payload["id"]),
        str(payload.get("recurrence_key") or ""),
    )


# ------------------------------------------------------------- recurrence glue


def _normalize_rrule(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("rrule must be a non-empty string or null")
    text = value.strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :].strip()
    try:
        parse_rule(text)
    except UnsupportedRecurrence as exc:
        raise UnsupportedRecurrenceError(str(exc)) from exc
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    return text


def _normalize_recurrence_value(value: Any, *, all_day: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("recurrence values must be non-empty strings")
    text = value.strip()
    token = text.split("/", 1)[0].strip()
    if not token:
        raise InvalidArgument(f"malformed recurrence value: {value!r}")
    if all_day:
        try:
            if _DATE_ONLY_RE.fullmatch(token):
                return date.fromisoformat(token).isoformat()
            if _COMPACT_DATE_RE.fullmatch(token):
                return datetime.strptime(token, "%Y%m%d").date().isoformat()
            if _DATE_TIME_RE.fullmatch(token) or _COMPACT_DATE_TIME_RE.fullmatch(token):
                parsed = _parse_iso_datetime(token)
                return parsed.date().isoformat()
        except ValueError as exc:
            raise InvalidArgument(f"malformed recurrence date: {value!r}") from exc
        raise InvalidArgument(f"malformed recurrence date: {value!r}")

    try:
        parsed = _parse_iso_datetime(token)
    except ValueError as exc:
        raise InvalidArgument(f"malformed recurrence datetime: {value!r}") from exc
    if parsed.tzinfo is not None:
        utc = parsed.astimezone(UTC)
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return parsed.strftime(_LOCAL_FORMAT)


def _parse_iso_datetime(token: str) -> datetime:
    if _COMPACT_DATE_TIME_RE.fullmatch(token):
        if token.upper().endswith("Z"):
            return datetime.strptime(token.upper(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return datetime.strptime(token.upper(), "%Y%m%dT%H%M%S")
    text = token
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)


def _recurrence_values(value: Any, name: str, *, all_day: bool) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw_values: Sequence[Any] = (value,)
    elif isinstance(value, list | tuple):
        raw_values = value
    else:
        raise InvalidArgument(f"{name} must be an array of strings or null")
    normalized = tuple(_normalize_recurrence_value(item, all_day=all_day) for item in raw_values)
    return normalized or None


def _engine_recurrence_values(values: tuple[str, ...] | None, *, all_day: bool) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(_engine_recurrence_value(value, all_day=all_day) for value in values)


def _engine_recurrence_value(value: str, *, all_day: bool) -> str:
    if all_day:
        return date.fromisoformat(_normalize_recurrence_value(value, all_day=True)).strftime(
            "%Y%m%d"
        )
    normalized = _normalize_recurrence_value(value, all_day=False)
    parsed = _parse_iso_datetime(normalized)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return parsed.strftime("%Y%m%dT%H%M%S")


def _master_event(event: Event) -> MasterEvent:
    rule = None if event.rrule_unsupported else event.rrule
    common: dict[str, Any] = {
        "rrule": rule,
        "rdate": _engine_recurrence_values(event.rdate, all_day=event.is_all_day),
        "exdate": _engine_recurrence_values(event.exdate, all_day=event.is_all_day),
    }
    if event.is_all_day:
        return MasterEvent(
            start_date=date.fromisoformat(str(event.start_date)),
            end_date=date.fromisoformat(str(event.end_date)),
            **common,
        )
    return MasterEvent(
        start_local=datetime.fromisoformat(str(event.start_local)),
        start_tz=event.start_tz,
        start_utc=datetime.fromtimestamp(event.start_utc, UTC),
        duration_seconds=event.duration_seconds
        if event.duration_seconds is not None
        else event.end_utc - event.start_utc,
        **common,
    )


def _override_event(event: Event) -> OverrideEvent:
    assert event.recurrence_key is not None  # schema ties overrides to a recurrence key
    common: dict[str, Any] = {"recurrence_key": event.recurrence_key, "status": event.status}
    if event.is_all_day:
        return OverrideEvent(
            start_date=date.fromisoformat(str(event.start_date)),
            end_date=date.fromisoformat(str(event.end_date)),
            **common,
        )
    return OverrideEvent(
        start_local=datetime.fromisoformat(str(event.start_local)),
        start_tz=event.start_tz,
        start_utc=datetime.fromtimestamp(event.start_utc, UTC),
        duration_seconds=event.duration_seconds
        if event.duration_seconds is not None
        else event.end_utc - event.start_utc,
        **common,
    )


def _expand_master(
    master: Event,
    overrides: Sequence[Event],
    start_utc: int,
    end_utc: int,
    warnings: list[str],
) -> list[Occurrence]:
    """Expand one master, degrading to DTSTART for unsupported rules."""
    if master.rrule_unsupported:
        warnings.append(
            f"{master.id}: unsupported RRULE part(s) "
            f"{', '.join(master.rrule_unsupported)}; showing DTSTART only"
        )
    try:
        override_values = [_override_event(item) for item in overrides]
        return expand(_master_event(master), start_utc, end_utc, override_values)
    except (
        UnsupportedRecurrence,
        ValueError,
        ServiceError,
        ExpansionLimitExceeded,
        ZoneInfoNotFoundError,
    ) as exc:
        warnings.append(f"{master.id}: recurrence not expanded ({exc}); showing DTSTART only")
        try:
            fallback = replace(_master_event(master), rrule=None, rdate=(), exdate=())
            override_values = [_override_event(item) for item in overrides]
            return expand(fallback, start_utc, end_utc, override_values)
        except (
            UnsupportedRecurrence,
            ValueError,
            ServiceError,
            ExpansionLimitExceeded,
            ZoneInfoNotFoundError,
        ):
            return []


def _occurrence_payload(
    master: Event,
    occurrence: Occurrence,
    overrides: Mapping[str, Event],
) -> dict[str, Any]:
    override = overrides.get(occurrence.recurrence_key)
    row = master if override is None else override
    payload = _event_payload(row)
    start_utc = int(occurrence.start_utc.timestamp())
    end_utc = int(occurrence.end_utc.timestamp())
    if occurrence.is_all_day:
        start_payload = _time_payload(
            start_utc,
            None,
            None,
            occurrence.start_date.isoformat() if occurrence.start_date else None,
        )
        end_payload = _time_payload(
            end_utc, None, None, occurrence.end_date.isoformat() if occurrence.end_date else None
        )
    else:
        zone = row.start_tz or master.start_tz
        start_payload = _time_payload(
            start_utc,
            occurrence.start_local.strftime(_LOCAL_FORMAT) if occurrence.start_local else None,
            zone,
            None,
        )
        end_payload = _time_payload(
            end_utc,
            occurrence.end_local.strftime(_LOCAL_FORMAT) if occurrence.end_local else None,
            zone,
            None,
        )
    payload.update(
        {
            "start": start_payload,
            "end": end_payload,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "duration_seconds": None if occurrence.is_all_day else end_utc - start_utc,
            "all_day": occurrence.is_all_day,
            "master_id": master.id,
            "is_override": override is not None,
            "override_event_id": override.id if override is not None else None,
            "occurrence_start": start_payload,
            "occurrence_end": end_payload,
            "occurrence_start_utc": start_utc,
            "occurrence_end_utc": end_utc,
            "recurrence_key": occurrence.recurrence_key,
        }
    )
    return payload


def _event_payload_with_own_occurrence(event: Event) -> dict[str, Any]:
    payload = _event_payload(event)
    payload.update(
        {
            "master_id": event.id,
            "is_override": False,
            "override_event_id": None,
            "occurrence_start": payload["start"],
            "occurrence_end": payload["end"],
            "occurrence_start_utc": event.start_utc,
            "occurrence_end_utc": event.end_utc,
            "recurrence_key": event.recurrence_key,
        }
    )
    return payload


def _list_calendar_events(
    repository: Repository,
    calendar_id: str,
    start_utc: int,
    end_utc: int,
    warnings: list[str],
) -> list[dict[str, Any]]:
    masters = repository.list_masters(calendar_id)
    master_ids = {master.id for master in masters}
    payloads = [
        _event_payload_with_own_occurrence(event)
        for event in repository.list_events(calendar_id, start_utc, end_utc)
        if event.id not in master_ids
    ]
    for master in masters:
        overrides = [
            item for item in repository.list_overrides(master.id) if item.deleted_utc is None
        ]
        by_key = {
            str(item.recurrence_key): item for item in overrides if item.recurrence_key is not None
        }
        for occurrence in _expand_master(master, overrides, start_utc, end_utc, warnings):
            payloads.append(_occurrence_payload(master, occurrence, by_key))
    return payloads


def _has_recurring_master(repository: Repository, calendar_id: str) -> bool:
    return bool(repository.list_masters(calendar_id))


def _require_event(repository: Repository, event_id: str) -> Event:
    event = repository.get_event(event_id)
    if event is None or event.deleted_utc is not None:
        raise EventNotFoundError(event_id)
    return event


def _require_calendar(repository: Repository, calendar_id: str) -> Calendar:
    for calendar in repository.list_calendars():
        if calendar.id == calendar_id:
            return calendar
    raise CalendarNotFoundError(calendar_id)


def _master_of(repository: Repository, event: Event) -> Event:
    if event.parent_id is None:
        if event.deleted_utc is not None:
            raise EventNotFoundError(event.id)
        return event
    master = repository.get_event(event.parent_id)
    if master is None or master.deleted_utc is not None:
        raise EventNotFoundError(event.parent_id)
    return master


# --------------------------------------------------------------- series edits


def _series_changes(requested: _ChangeMap, master: Event) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "summary" in requested:
        changes["summary"] = _required_change_str(requested["summary"], "summary")
    for field in ("description", "location", "url"):
        if field in requested:
            changes[field] = _optional_str(requested[field], field)
    if "rrule" in requested:
        changes["rrule"] = _normalize_rrule(requested["rrule"])
        changes["rrule_unsupported"] = None
    if "rdates" in requested:
        changes["rdate"] = _recurrence_values(
            requested["rdates"], "rdates", all_day=master.is_all_day
        )
    if "exdates" in requested:
        changes["exdate"] = _recurrence_values(
            requested["exdates"], "exdates", all_day=master.is_all_day
        )
    if "start" in requested or "end" in requested:
        if master.is_all_day:
            raise InvalidArgument("start and end edits are not supported for an all-day series")
        changes.update(_updated_times(requested, master))
    return changes


def _occurrence_changes(requested: _ChangeMap, base: Event) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "summary" in requested:
        changes["summary"] = _required_change_str(requested["summary"], "summary")
    for field in ("description", "location", "url"):
        if field in requested:
            changes[field] = _optional_str(requested[field], field)
    if "status" in requested:
        status = requested["status"]
        if not isinstance(status, str) or status.upper() not in {
            "CONFIRMED",
            "TENTATIVE",
            "CANCELLED",
        }:
            raise InvalidArgument("status must be CONFIRMED, TENTATIVE, or CANCELLED")
        changes["status"] = status.upper()
    if "start" in requested or "end" in requested:
        if base.is_all_day:
            raise InvalidArgument("start and end edits are not supported for an all-day occurrence")
        changes.update(_updated_times(requested, base))
    return changes


def _required_change_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(f"{name} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise InvalidArgument(f"{name} must be at most {MAX_TEXT} characters")
    return value.strip()


def _updated_times(requested: _ChangeMap, current: Event) -> dict[str, Any]:
    provided_tz = _validated_zone_arg(requested.get("timezone"), "timezone")
    keep_floating = current.is_floating and provided_tz is None
    if provided_tz is not None:
        zone = _zone(provided_tz)
    elif current.is_floating:
        zone = _zone("UTC")
    else:
        zone = _zone(current.start_tz or _default_timezone_name())
    if "start" in requested:
        start = _parse_time(requested.get("start"), "start", zone)
    else:
        start = _time_from_utc(current.start_utc, zone)
    if "end" in requested:
        end = _parse_time(requested.get("end"), "end", zone)
    else:
        duration = current.duration_seconds or (current.end_utc - current.start_utc)
        end = _time_from_utc(start.utc + duration, zone)
    _validate_span(start, end)
    values: dict[str, Any] = {
        "start_utc": start.utc,
        "start_local": start.local,
        "start_tz": None if keep_floating else start.tz,
        "end_utc": end.utc,
        "end_local": end.local,
        "end_tz": None if keep_floating else end.tz,
        "duration_seconds": end.utc - start.utc,
    }
    if current.is_floating and provided_tz is not None:
        values["is_floating"] = False
    return values


def _occurrence_for(
    repository: Repository,
    master: Event,
    occurrence_raw: Any,
) -> Occurrence:
    key, window_start, window_end = _occurrence_window(master, occurrence_raw)
    occurrences = _expand_master(master, (), window_start, window_end, [])
    matches = [item for item in occurrences if item.recurrence_key == key]
    if not matches:
        raise InvalidArgument("occurrence_start is not an occurrence of this series")
    return matches[0]


def _occurrence_window(master: Event, occurrence_raw: Any) -> tuple[str, int, int]:
    if master.is_all_day:
        if not isinstance(occurrence_raw, str) or not occurrence_raw.strip():
            raise InvalidArgument(
                "occurrence_start is required and must be an ISO date for an all-day series"
            )
        try:
            day = date.fromisoformat(_normalize_recurrence_value(occurrence_raw, all_day=True))
        except InvalidArgument as exc:
            raise InvalidArgument(
                f"occurrence_start must be an ISO date for an all-day series; got {occurrence_raw!r}"
            ) from exc
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return (
            all_day_recurrence_key(day),
            int(start.timestamp()),
            int((start + timedelta(days=1)).timestamp()),
        )
    zone_name = master.start_tz
    if zone_name is None:
        zone_name = "UTC" if master.is_floating else _default_timezone_name()
    zone = _zone(zone_name)
    value = _parse_time(occurrence_raw, "occurrence_start", zone)
    return (
        timed_recurrence_key(datetime.fromtimestamp(value.utc, UTC)),
        value.utc - 1,
        value.utc + 1,
    )


def _occurrence_base(master: Event, original: Occurrence, existing: Event | None) -> Event:
    """Build the full event values an override starts from.

    A derived event is not stored; ``_event_payload`` only needs an :class:`Event`
    shape, so the master supplies the stable columns and the occurrence supplies
    its own start/end.
    """
    if existing is not None:
        return existing
    base = replace(
        master,
        id="",
        parent_id=master.id,
        recurrence_key=original.recurrence_key,
        recurrence_range=None,
        rev=1,
    )
    if original.is_all_day:
        return replace(
            base,
            start_date=original.start_date.isoformat() if original.start_date else None,
            end_date=original.end_date.isoformat() if original.end_date else None,
            start_local=None,
            end_local=None,
            start_tz=None,
            end_tz=None,
            duration_seconds=None,
            start_utc=int(original.start_utc.timestamp()),
            end_utc=int(original.end_utc.timestamp()),
            is_all_day=True,
            is_floating=False,
        )
    return replace(
        base,
        start_utc=int(original.start_utc.timestamp()),
        end_utc=int(original.end_utc.timestamp()),
        start_local=original.start_local.strftime(_LOCAL_FORMAT) if original.start_local else None,
        end_local=original.end_local.strftime(_LOCAL_FORMAT) if original.end_local else None,
        start_tz=master.start_tz,
        end_tz=master.start_tz,
        duration_seconds=int((original.end_utc - original.start_utc).total_seconds()),
        is_all_day=False,
        is_floating=master.is_floating,
        start_date=None,
        end_date=None,
    )


def _recurrence_id_raw(occurrence: Occurrence, master: Event) -> str:
    if occurrence.is_all_day:
        return (
            occurrence.start_date.isoformat()
            if occurrence.start_date is not None
            else str(master.start_date)
        )
    assert occurrence.start_local is not None  # timed occurrences always have it
    return occurrence.start_local.strftime(_LOCAL_FORMAT)


def _draft_from_event(event: Event, *, overrides: Mapping[str, Any] | None = None) -> EventDraft:
    values: dict[str, Any] = {
        "calendar_id": event.calendar_id,
        "start_utc": event.start_utc,
        "end_utc": event.end_utc,
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "url": event.url,
        "start_local": event.start_local,
        "start_tz": event.start_tz,
        "end_local": event.end_local,
        "end_tz": event.end_tz,
        "duration_seconds": event.duration_seconds,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "is_all_day": event.is_all_day,
        "is_floating": event.is_floating,
        "id": event.id or None,
        "uid": event.uid,
        "owner_profile_id": event.owner_profile_id,
        "remote_href": event.remote_href,
        "etag": event.etag,
        "sequence": event.sequence,
        "rrule": event.rrule,
        "rrule_unsupported": event.rrule_unsupported,
        "rdate": event.rdate,
        "exdate": event.exdate,
        "parent_id": event.parent_id,
        "recurrence_key": event.recurrence_key,
        "recurrence_id_raw": event.recurrence_id_raw,
        "recurrence_range": event.recurrence_range,
        "status": event.status,
        "transparency": event.transparency,
        "classification": event.classification,
        "organizer_email": event.organizer_email,
        "organizer_name": event.organizer_name,
        "categories": event.categories,
        "xprops": dict(event.xprops),
        "created_utc": event.created_utc,
        "dtstamp_utc": event.dtstamp_utc,
        "last_modified_utc": event.last_modified_utc,
        "attendees": event.attendees,
        "reminders": event.reminders,
    }
    if overrides:
        values.update(overrides)
    return EventDraft(**values)


def _event_to_draft(event: Event) -> EventDraft:
    return _draft_from_event(event)


def _series_overlaps(
    master: Event,
    overrides: Sequence[Event],
    start_utc: int,
    end_utc: int,
) -> bool:
    return bool(_expand_master(master, overrides, start_utc, end_utc, []))


# --------------------------------------------------------------- ICS mappings


def _prepare_master(
    draft: EventDraft, calendar_id: str, warnings: list[str]
) -> tuple[EventDraft, list[str]]:
    prepared = replace(draft, calendar_id=calendar_id, parent_id=None)
    notes = list(warnings)
    computed = _unsupported_parts(prepared.rrule) if prepared.rrule is not None else ()
    unsupported = tuple(sorted({*(prepared.rrule_unsupported or ()), *computed}))
    if unsupported:
        prepared = replace(prepared, rrule_unsupported=unsupported)
        notes.append(
            f"{prepared.uid}: unsupported RRULE part(s) {', '.join(unsupported)}; "
            "stored for round-trip but not expanded"
        )
    return prepared, notes


def _prepare_override(
    draft: EventDraft, master: Event, warnings: list[str]
) -> tuple[EventDraft, list[str]]:
    prepared = replace(
        draft,
        calendar_id=master.calendar_id,
        uid=master.uid,
        parent_id=master.id,
        rrule=None,
        rrule_unsupported=None,
        rdate=None,
        exdate=None,
    )
    return prepared, list(warnings)


def _unsupported_parts(rrule: str) -> tuple[str, ...]:
    text = rrule.strip()
    parts: list[str] = []
    frequency: str | None = None
    for raw in text.split(";"):
        name, separator, value = raw.partition("=")
        name = name.strip().upper()
        if not separator or not name:
            continue
        if name not in P0_RULE_PARTS:
            parts.append(raw.strip().upper())
        elif name == "FREQ":
            frequency = value.strip().upper()
    if frequency is not None and frequency not in SUPPORTED_FREQUENCIES:
        parts.append(f"FREQ={frequency}")
    return tuple(sorted(set(parts)))


def _apply_draft(current: Event, draft: EventDraft) -> Event:
    return replace(
        current,
        summary=draft.summary,
        description=draft.description,
        location=draft.location,
        url=draft.url,
        start_utc=draft.start_utc,
        end_utc=draft.end_utc,
        start_local=draft.start_local,
        start_tz=draft.start_tz,
        end_local=draft.end_local,
        end_tz=draft.end_tz,
        duration_seconds=draft.duration_seconds,
        start_date=draft.start_date,
        end_date=draft.end_date,
        is_all_day=draft.is_all_day,
        is_floating=draft.is_floating,
        sequence=draft.sequence,
        rrule=draft.rrule,
        rrule_unsupported=draft.rrule_unsupported,
        rdate=draft.rdate,
        exdate=draft.exdate,
        recurrence_key=draft.recurrence_key,
        recurrence_id_raw=draft.recurrence_id_raw,
        recurrence_range=draft.recurrence_range,
        status=draft.status,
        transparency=draft.transparency,
        classification=draft.classification,
        organizer_email=draft.organizer_email,
        organizer_name=draft.organizer_name,
        categories=draft.categories,
        xprops=dict(draft.xprops),
        created_utc=draft.created_utc if draft.created_utc is not None else current.created_utc,
        dtstamp_utc=draft.dtstamp_utc,
        last_modified_utc=draft.last_modified_utc,
        attendees=draft.attendees,
        reminders=draft.reminders,
        deleted_utc=None,
    )
