"""Agent-facing JSON adapters over :mod:`hermes_calendar.service`.

Contract verified against Hermes Agent v0.23.0 on ``docker-host``:

* ``PluginContext.register_tool`` takes ``name``, ``toolset``, ``schema``, and
  ``handler`` (plus optional gating arguments). Every name must also appear in
  ``plugin.yaml`` ``provides_tools`` or ``hermes plugins validate`` reports
  drift.
* ``ToolRegistry.dispatch`` calls a synchronous handler as
  ``handler(args, **kwargs)``; optional context kwargs are filtered by
  signature, so every handler accepts and ignores ``**_kwargs``.
* Handlers return a JSON string. Success is ``{"ok": true, ...}`` and failure is
  ``{"ok": false, "error": ..., "code": ...}``. Nothing raises to Hermes.

All calendar policy lives in :class:`~hermes_calendar.service.CalendarService`.
This module only decodes arguments, calls one service method, and maps
exceptions onto the tool envelope.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from typing import Any

from ..db import (
    DatabaseError,
    NotFoundError,
    Repository,
    RevisionConflict,
    open_global_store,
    open_profile_store,
)
from ..reminders import service as reminder_service
from ..service import (
    DEFAULT_SCOPE,
    MAX_TEXT,
    CalendarService,
    Conflict,
    InvalidArgument,
    Scope,
    ServiceError,
)

PLUGIN_NAME = "hermes-calendar"
TOOLSET = "calendar"
_TYPE = "object"
_MAX_TEXT = MAX_TEXT

ToolHandler = Callable[[Mapping[str, Any]], dict[str, Any]]

# Backwards-compatible names for callers that imported the old error classes.
ToolError = ServiceError
ToolArgumentError = InvalidArgument
ToolConflictError = Conflict


def _json(payload: Mapping[str, Any]) -> str:
    """Serialize a tool result deterministically."""
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)


def error_payload(exc: BaseException, *, expose_details: bool = True) -> dict[str, Any]:
    """Map an exception onto the plugin's structured error envelope.

    The tools keep the historic behaviour of including store and unexpected
    exception text. Callers that face an untrusted client (the dashboard API)
    pass ``expose_details=False`` so a raw exception is never echoed back.
    """
    if isinstance(exc, ServiceError):
        return {"ok": False, "error": str(exc), "code": exc.code}
    if isinstance(exc, RevisionConflict):
        return {
            "ok": False,
            "error": str(exc),
            "code": "revision_conflict",
            "event_id": exc.event_id,
            "expected_rev": exc.expected_rev,
        }
    if isinstance(exc, NotFoundError):
        return {
            "ok": False,
            "error": str(exc),
            "code": "not_found",
            "error_type": type(exc).__name__,
        }
    if isinstance(exc, sqlite3.IntegrityError):
        detail = f": {exc}" if expose_details else ""
        return {
            "ok": False,
            "error": f"database constraint rejected the change{detail}",
            "code": "conflict",
        }
    if isinstance(exc, DatabaseError):
        return {
            "ok": False,
            "error": str(exc) if expose_details else "the calendar store is unavailable",
            "code": "store_error",
        }
    if expose_details:
        return {
            "ok": False,
            "error": f"unexpected error: {type(exc).__name__}: {exc}",
            "code": "internal_error",
        }
    return {"ok": False, "error": "internal error", "code": "internal_error"}


def _tool(handler: ToolHandler) -> Callable[..., str]:
    """Wrap a handler so it always returns JSON and never raises to Hermes."""

    @wraps(handler)
    def wrapped(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        try:
            if args is None:
                values: Mapping[str, Any] = {}
            elif isinstance(args, Mapping):
                values = args
            else:
                raise InvalidArgument("arguments must be a JSON object")
            payload = handler(values)
            payload.setdefault("ok", True)
            return _json(payload)
        except Exception as exc:  # noqa: BLE001 - the plugin contract forbids raising into Hermes
            return _json(error_payload(exc))

    return wrapped


@contextmanager
def _tool_store(scope: Scope) -> Iterator[Repository]:
    """Open exactly one store, keeping the historic monkeypatch seam in tools."""
    connection = open_profile_store() if scope == "profile" else open_global_store()
    try:
        yield Repository(connection)
    finally:
        connection.close()


def _service() -> CalendarService:
    """Build a service bound to this module's store opener."""
    return CalendarService(open_store=_tool_store)


def _scope_parameter() -> dict[str, Any]:
    """Schema fragment shared by every tool."""
    return {
        "type": "string",
        "enum": ["profile", "global"],
        "default": DEFAULT_SCOPE,
        "description": (
            "Store to use: 'profile' (default, private to the active Hermes profile) "
            "or 'global' (the shared calendar)."
        ),
    }


def _timezone_parameter() -> dict[str, Any]:
    """Schema fragment for an optional IANA zone."""
    return {
        "type": "string",
        "description": (
            "IANA time zone used to interpret datetimes that have no offset, "
            "for example America/New_York. Defaults to the profile's configured "
            "timezone, else UTC."
        ),
    }


def _recurrence_value_parameter(name: str) -> dict[str, Any]:
    """Schema fragment for an RFC 5545 RDATE/EXDATE list."""
    return {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "description": (
            f"Optional {name} values: ISO dates for all-day events or ISO datetimes "
            "for timed events. PERIOD values are reduced to their start."
        ),
    }


def _series_scope_parameter() -> dict[str, Any]:
    """Schema fragment for whole-series versus one-occurrence edits."""
    return {
        "type": "string",
        "enum": ["this", "all"],
        "description": (
            "Edit only the occurrence named by occurrence_start ('this') or the whole "
            "series ('all'). Defaults to 'this' when occurrence_start is supplied, "
            "otherwise 'all'."
        ),
    }


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the function-spec shape Hermes expects."""
    parameters: dict[str, Any] = {
        "type": _TYPE,
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = list(required)
    return {"name": name, "description": description, "parameters": parameters}


_LIST_CALENDARS_SCHEMA = _schema(
    "calendar_list_calendars",
    "List calendars in the selected store with their IDs, names, colors, default "
    "timezone, and read-only flag. The global store normally contains exactly one "
    "calendar; the profile store starts empty until a calendar is created.",
    {"scope": _scope_parameter()},
)

_CREATE_CALENDAR_SCHEMA = _schema(
    "calendar_create_calendar",
    "Create a calendar in the selected store. The global store already has its one "
    "shared calendar, so creating another there is rejected. Calendar names are not "
    "required to be unique.",
    {
        "name": {"type": "string", "description": "Calendar name. Required."},
        "description": {"type": "string", "description": "Optional description."},
        "color": {"type": "string", "description": "Optional color token, e.g. #336699."},
        "default_tz": {
            "type": "string",
            "description": (
                "IANA zone for this calendar's default timezone. Defaults to the "
                "profile timezone, else UTC."
            ),
        },
        "read_only": {
            "type": "boolean",
            "description": "Mark the calendar read-only. Default false.",
        },
        "scope": _scope_parameter(),
    },
    ("name",),
)

_CREATE_EVENT_SCHEMA = _schema(
    "calendar_create_event",
    "Create a timed event in an existing calendar. Times are ISO 8601 datetimes; a "
    "value with Z or a numeric offset is absolute, while a value without an offset "
    "is interpreted in the timezone argument or the profile's timezone. Supply an "
    "rrule (and optional rdates/exdates) to create a recurring series; unsupported "
    "RRULE parts are rejected. Returns the stored event including its id and rev; "
    "pass rev as expected_rev to update it.",
    {
        "calendar_id": {"type": "string", "description": "Target calendar ID. Required."},
        "summary": {"type": "string", "description": "Event title. Required."},
        "start": {
            "type": "string",
            "description": "ISO 8601 start datetime, e.g. 2026-03-02T09:00:00-05:00.",
        },
        "end": {
            "type": "string",
            "description": "ISO 8601 end datetime. Must be later than start.",
        },
        "timezone": _timezone_parameter(),
        "description": {
            "type": ["string", "null"],
            "description": "Optional event description.",
        },
        "location": {"type": ["string", "null"], "description": "Optional location."},
        "url": {"type": ["string", "null"], "description": "Optional URL."},
        "rrule": {
            "type": ["string", "null"],
            "description": (
                "Optional RFC 5545 RRULE without the 'RRULE:' prefix, for example "
                "FREQ=WEEKLY;BYDAY=MO. Only FREQ, INTERVAL, COUNT, UNTIL, BYDAY, "
                "BYMONTHDAY, BYMONTH, BYSETPOS, and WKST are supported."
            ),
        },
        "rdates": _recurrence_value_parameter("RDATE"),
        "exdates": _recurrence_value_parameter("EXDATE"),
        "scope": _scope_parameter(),
    },
    ("calendar_id", "summary", "start", "end"),
)

_GET_EVENT_SCHEMA = _schema(
    "calendar_get_event",
    "Return one event by ID from the selected store, including its revision, times, "
    "recurrence fields, and status. A deleted or missing event returns not_found.",
    {
        "event_id": {"type": "string", "description": "Event ID. Required."},
        "scope": _scope_parameter(),
    },
    ("event_id",),
)

_UPDATE_EVENT_SCHEMA = _schema(
    "calendar_update_event",
    "Update event fields using optimistic concurrency. expected_rev must match the "
    "current master event rev; a stale value returns a structured revision_conflict "
    "error and changes nothing. Pass occurrence_start (or series_scope='this') to "
    "edit one instance of a recurring series as an override, or series_scope='all' "
    "to change the whole series. Times in the update follow the same ISO 8601 rules "
    "as creation; changing only start preserves duration.",
    {
        "event_id": {"type": "string", "description": "Event ID. Required."},
        "expected_rev": {
            "type": "integer",
            "minimum": 1,
            "description": "Revision of the master event this update is based on.",
        },
        "summary": {"type": "string", "description": "New event title."},
        "description": {
            "type": ["string", "null"],
            "description": "New description, or null.",
        },
        "location": {"type": ["string", "null"], "description": "New location, or null."},
        "url": {"type": ["string", "null"], "description": "New URL, or null."},
        "start": {"type": "string", "description": "New ISO 8601 start datetime."},
        "end": {"type": "string", "description": "New ISO 8601 end datetime."},
        "timezone": _timezone_parameter(),
        "rrule": {
            "type": ["string", "null"],
            "description": "New whole-series RRULE, or null to clear it.",
        },
        "rdates": _recurrence_value_parameter("RDATE"),
        "exdates": _recurrence_value_parameter("EXDATE"),
        "occurrence_start": {
            "type": "string",
            "description": (
                "Original start of the occurrence to edit, in the series timezone "
                "(shown as occurrence_start.local by calendar_list_events)."
            ),
        },
        "series_scope": _series_scope_parameter(),
        "scope": _scope_parameter(),
    },
    ("event_id", "expected_rev"),
)

_DELETE_EVENT_SCHEMA = _schema(
    "calendar_delete_event",
    "Soft-delete an event in the selected store. Deleting an already deleted event "
    "is a successful no-op. An optional occurrence_start cancels only that instance "
    "of a recurring series; series_scope='all' deletes the whole series and its "
    "overrides. An optional expected_rev uses the same compare-and-swap as update.",
    {
        "event_id": {"type": "string", "description": "Event ID. Required."},
        "expected_rev": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional master revision this delete is based on.",
        },
        "occurrence_start": {
            "type": "string",
            "description": "Original start of the one occurrence to cancel.",
        },
        "series_scope": _series_scope_parameter(),
        "scope": _scope_parameter(),
    },
    ("event_id",),
)

_LIST_EVENTS_SCHEMA = _schema(
    "calendar_list_events",
    "List events overlapping a UTC instant range [start, end). Recurring series are "
    "expanded into occurrences that carry occurrence_start, occurrence_end, "
    "master_id, recurrence_key, and is_override. EXDATE values and per-instance "
    "overrides are applied. Times use the same ISO 8601 rules as creation. Omit "
    "calendar_id to merge every calendar in the selected store.",
    {
        "start": {"type": "string", "description": "ISO 8601 range start (inclusive)."},
        "end": {"type": "string", "description": "ISO 8601 range end (exclusive)."},
        "timezone": _timezone_parameter(),
        "calendar_id": {
            "type": "string",
            "description": "Optional calendar ID; defaults to every calendar in the store.",
        },
        "scope": _scope_parameter(),
    },
    ("start", "end"),
)

_IMPORT_ICS_SCHEMA = _schema(
    "calendar_import_ics",
    "Import iCalendar (RFC 5545) text into a calendar. Import is idempotent by UID: "
    "re-importing the same document updates the existing master and overrides "
    "instead of duplicating them. Returns imported/updated/skipped counts and "
    "parser warnings. The text must already be in hand; this tool never fetches a "
    "URL or follows a link.",
    {
        "text": {
            "type": "string",
            "description": "Complete iCalendar text beginning with BEGIN:VCALENDAR.",
        },
        "calendar_id": {"type": "string", "description": "Target calendar ID. Required."},
        "scope": _scope_parameter(),
    },
    ("text", "calendar_id"),
)

_EXPORT_ICS_SCHEMA = _schema(
    "calendar_export_ics",
    "Export a calendar window as deterministic iCalendar text. Recurring series are "
    "exported as masters plus overrides (with RRULE/RDATE/EXDATE), not as expanded "
    "instances, so subscribers and re-import update rather than duplicate. The "
    "result includes the ics text and the number of stored rows exported.",
    {
        "calendar_id": {"type": "string", "description": "Calendar ID. Required."},
        "start": {"type": "string", "description": "ISO 8601 range start (inclusive)."},
        "end": {"type": "string", "description": "ISO 8601 range end (exclusive)."},
        "timezone": _timezone_parameter(),
        "scope": _scope_parameter(),
    },
    ("calendar_id", "start", "end"),
)


def _changes(args: Mapping[str, Any]) -> dict[str, Any]:
    """Remove routing/control keys before handing fields to the service."""
    control = {"event_id", "expected_rev", "scope", "occurrence_start", "series_scope"}
    return {key: value for key, value in args.items() if key not in control}


@_tool
def calendar_list_calendars(args: Mapping[str, Any]) -> dict[str, Any]:
    """List calendars in the selected store."""
    return _service().list_calendars(scope=args.get("scope", DEFAULT_SCOPE))


@_tool
def calendar_create_calendar(args: Mapping[str, Any]) -> dict[str, Any]:
    """Create a calendar in the selected store."""
    return _service().create_calendar(
        scope=args.get("scope", DEFAULT_SCOPE),
        name=args.get("name"),
        description=args.get("description"),
        color=args.get("color"),
        default_tz=args.get("default_tz"),
        read_only=args.get("read_only"),
    )


@_tool
def calendar_create_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Create a timed event, optionally recurring, in an existing calendar."""
    return _service().create_event(
        scope=args.get("scope", DEFAULT_SCOPE),
        calendar_id=args.get("calendar_id"),
        summary=args.get("summary"),
        start=args.get("start"),
        end=args.get("end"),
        timezone=args.get("timezone"),
        description=args.get("description"),
        location=args.get("location"),
        url=args.get("url"),
        rrule=args.get("rrule"),
        rdates=args.get("rdates"),
        exdates=args.get("exdates"),
    )


@_tool
def calendar_get_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Return one non-deleted event by ID."""
    return _service().get_event(
        scope=args.get("scope", DEFAULT_SCOPE),
        event_id=args.get("event_id"),
    )


@_tool
def calendar_update_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Update a whole series or one occurrence with compare-and-swap."""
    return _service().update_event(
        scope=args.get("scope", DEFAULT_SCOPE),
        event_id=args.get("event_id"),
        expected_rev=args.get("expected_rev"),
        changes=_changes(args),
        occurrence_start=args.get("occurrence_start"),
        series_scope=args.get("series_scope"),
    )


@_tool
def calendar_delete_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Soft-delete one event, one occurrence, or a whole series."""
    return _service().delete_event(
        scope=args.get("scope", DEFAULT_SCOPE),
        event_id=args.get("event_id"),
        expected_rev=args.get("expected_rev"),
        occurrence_start=args.get("occurrence_start"),
        series_scope=args.get("series_scope"),
    )


@_tool
def calendar_list_events(args: Mapping[str, Any]) -> dict[str, Any]:
    """List events and expanded recurrence occurrences overlapping a range."""
    return _service().list_events(
        scope=args.get("scope", DEFAULT_SCOPE),
        start=args.get("start"),
        end=args.get("end"),
        timezone=args.get("timezone"),
        calendar_id=args.get("calendar_id"),
    )


@_tool
def calendar_import_ics(args: Mapping[str, Any]) -> dict[str, Any]:
    """Import iCalendar text idempotently by UID."""
    return _service().import_ics(
        scope=args.get("scope", DEFAULT_SCOPE),
        calendar_id=args.get("calendar_id"),
        text=args.get("text"),
    )


@_tool
def calendar_export_ics(args: Mapping[str, Any]) -> dict[str, Any]:
    """Export a calendar window as iCalendar text."""
    return _service().export_ics(
        scope=args.get("scope", DEFAULT_SCOPE),
        calendar_id=args.get("calendar_id"),
        start=args.get("start"),
        end=args.get("end"),
        timezone=args.get("timezone"),
    )


_LIST_NOTIFICATIONS_SCHEMA = _schema(
    "calendar_list_notifications",
    "List reminders for the active profile, newest first. By default dismissed "
    "notifications are hidden. Each item contains only the event title, state, "
    "priority, times, and the latest delivery outcome; descriptions, locations, "
    "attendees, and message bodies are deliberately omitted. Use "
    "calendar_snooze_notification or calendar_dismiss_notification to act on one.",
    {
        "include_dismissed": {
            "type": "boolean",
            "description": "Include acknowledged notifications. Default false.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Maximum rows to return. Default 20.",
        },
        "state": {
            "type": "string",
            "enum": list(reminder_service.VISIBLE_STATES),
            "description": "Optional exact state filter.",
        },
    },
)

_SNOOZE_NOTIFICATION_SCHEMA = _schema(
    "calendar_snooze_notification",
    "Snooze a reminder so it fires again after 5, 15, 60, or 1440 minutes. The "
    "reminder returns to the scheduled state and a later tick delivers it once. "
    "Use the id from calendar_list_notifications.",
    {
        "notification_id": {
            "type": "string",
            "description": "Reminder instance id. Required.",
        },
        "minutes": {
            "type": "integer",
            "enum": sorted(reminder_service.SNOOZE_MINUTES),
            "description": "Snooze duration in minutes. Required.",
        },
    },
    ("notification_id", "minutes"),
)

_DISMISS_NOTIFICATION_SCHEMA = _schema(
    "calendar_dismiss_notification",
    "Dismiss (acknowledge) a reminder so it no longer appears in the default "
    "notification list. Dismissing an already dismissed reminder is a no-op. "
    "Use the id from calendar_list_notifications.",
    {
        "notification_id": {
            "type": "string",
            "description": "Reminder instance id. Required.",
        },
    },
    ("notification_id",),
)


def _require_str(args: Mapping[str, Any], name: str) -> str:
    """Return a required non-empty string notification argument."""
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(f"{name} is required and must be a non-empty string")
    if len(value) > _MAX_TEXT:
        raise InvalidArgument(f"{name} must be at most {_MAX_TEXT} characters")
    return value.strip()


def _optional_bool(args: Mapping[str, Any], name: str, *, default: bool) -> bool:
    """Return an optional boolean notification argument."""
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise InvalidArgument(f"{name} must be a boolean")
    return value


def _require_int(args: Mapping[str, Any], name: str, *, minimum: int) -> int:
    """Return a required integer notification argument at or above ``minimum``."""
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(f"{name} is required and must be an integer")
    if value < minimum:
        raise InvalidArgument(f"{name} must be at least {minimum}")
    return value


def _optional_int(args: Mapping[str, Any], name: str, *, minimum: int) -> int | None:
    """Return an optional integer notification argument at or above ``minimum``."""
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(f"{name} must be an integer")
    if value < minimum:
        raise InvalidArgument(f"{name} must be at least {minimum}")
    return value


def _notification_state(args: Mapping[str, Any]) -> str | None:
    value = args.get("state")
    if value is None:
        return None
    if not isinstance(value, str) or value not in reminder_service.VISIBLE_STATES:
        raise ToolArgumentError(
            "state must be one of "
            + ", ".join(repr(item) for item in reminder_service.VISIBLE_STATES)
        )
    return value


@_tool
def calendar_list_notifications(args: Mapping[str, Any]) -> dict[str, Any]:
    """List notification instances for the active profile."""
    include_dismissed = _optional_bool(args, "include_dismissed", default=False)
    limit = _optional_int(args, "limit", minimum=1)
    effective_limit = 20 if limit is None else min(limit, 100)
    state = _notification_state(args)
    with reminder_service.notification_stores() as (profile_connection, global_connection):
        notifications = reminder_service.list_notifications(
            profile_connection,
            global_connection=global_connection,
            include_dismissed=include_dismissed,
            limit=effective_limit,
            state=state,
        )
    return {"scope": "profile", "count": len(notifications), "notifications": notifications}


@_tool
def calendar_snooze_notification(args: Mapping[str, Any]) -> dict[str, Any]:
    """Snooze one notification for an allowlisted duration."""
    notification_id = _require_str(args, "notification_id")
    minutes = _require_int(args, "minutes", minimum=5)
    if minutes not in reminder_service.SNOOZE_MINUTES:
        allowed = ", ".join(str(item) for item in sorted(reminder_service.SNOOZE_MINUTES))
        raise ToolArgumentError(f"minutes must be one of {allowed}")
    with reminder_service.notification_stores() as (profile_connection, global_connection):
        notification = reminder_service.snooze_notification(
            profile_connection,
            notification_id=notification_id,
            minutes=minutes,
            global_connection=global_connection,
        )
    return {"scope": "profile", "notification": notification}


@_tool
def calendar_dismiss_notification(args: Mapping[str, Any]) -> dict[str, Any]:
    """Dismiss (acknowledge) one notification."""
    notification_id = _require_str(args, "notification_id")
    with reminder_service.notification_stores() as (profile_connection, global_connection):
        notification = reminder_service.dismiss_notification(
            profile_connection,
            notification_id=notification_id,
            global_connection=global_connection,
        )
    return {"scope": "profile", "notification": notification}


def _spec(
    schema: dict[str, Any], handler: Callable[..., str]
) -> tuple[str, dict[str, Any], Callable[..., str], str]:
    """Pair a schema with its handler for registration in ``plugin.py``."""
    return str(schema["name"]), schema, handler, str(schema["description"])


TOOL_SPECS: tuple[tuple[str, dict[str, Any], Callable[..., str], str], ...] = (
    _spec(_LIST_CALENDARS_SCHEMA, calendar_list_calendars),
    _spec(_CREATE_CALENDAR_SCHEMA, calendar_create_calendar),
    _spec(_CREATE_EVENT_SCHEMA, calendar_create_event),
    _spec(_GET_EVENT_SCHEMA, calendar_get_event),
    _spec(_UPDATE_EVENT_SCHEMA, calendar_update_event),
    _spec(_DELETE_EVENT_SCHEMA, calendar_delete_event),
    _spec(_LIST_EVENTS_SCHEMA, calendar_list_events),
    _spec(_IMPORT_ICS_SCHEMA, calendar_import_ics),
    _spec(_EXPORT_ICS_SCHEMA, calendar_export_ics),
    _spec(_LIST_NOTIFICATIONS_SCHEMA, calendar_list_notifications),
    _spec(_SNOOZE_NOTIFICATION_SCHEMA, calendar_snooze_notification),
    _spec(_DISMISS_NOTIFICATION_SCHEMA, calendar_dismiss_notification),
)

__all__ = [
    "TOOL_SPECS",
    "error_payload",
    "calendar_create_calendar",
    "calendar_create_event",
    "calendar_delete_event",
    "calendar_dismiss_notification",
    "calendar_export_ics",
    "calendar_get_event",
    "calendar_import_ics",
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_list_notifications",
    "calendar_snooze_notification",
    "calendar_update_event",
]
