"""Agent-facing calendar tools exposed through Hermes ``ctx``.

Contract verified against Hermes Agent v0.23.0 on ``docker-host``:

* ``PluginContext.register_tool`` in ``~/.hermes/hermes-agent/hermes_cli/plugins.py``
  takes ``name``, ``toolset``, ``schema``, and ``handler`` (plus optional
  gating arguments such as ``check_fn`` and ``requires_env``). The names must
  also appear in ``plugin.yaml`` ``provides_tools`` or ``hermes plugins
  validate`` reports drift.
* A tool ``schema`` is ``{"name", "description", "parameters"}`` where
  ``parameters`` is a JSON Schema object (see the bundled
  ``plugins/google_meet/tools.py`` on the same host).
* ``ToolRegistry.dispatch`` in ``~/.hermes/hermes-agent/tools/registry.py``
  calls a synchronous handler as ``handler(args, **kwargs)``; optional context
  kwargs are filtered by signature, so every handler here accepts and ignores
  ``**_kwargs``.
* Handlers return a string. The registry normalizes non-string results and
  converts exceptions into tool errors, but the documented plugin convention
  (and ``google_meet``) is to return JSON from the handler itself. Every
  handler in this module therefore returns a JSON string: success
  ``{"ok": true, ...}``, failure ``{"ok": false, "error": ..., "code": ...}``.
  Nothing raises to Hermes.

The database layer is the existing :mod:`hermes_calendar.db.repository`; these
handlers only validate and translate between JSON and the frozen models.

Time handling: inputs are ISO 8601 datetimes. A value with ``Z`` or a numeric
offset is an absolute instant; a value without an offset is interpreted in the
selected IANA zone (the ``timezone`` argument, else the profile's configured
zone, else UTC). Timed events always store the UTC instant plus the local wall
clock and IANA zone, per ``docs/design/core.md`` §3.4.

Recurrence expansion is intentionally absent in this milestone. The single
seam is :func:`_expand_events`; ``calendar_list_events`` returns exactly what
:meth:`Repository.list_events` returns and advertises that limitation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import (
    CalendarNotFoundError,
    DatabaseError,
    EventNotFoundError,
    NotFoundError,
    Repository,
    RevisionConflict,
    open_global_store,
    open_profile_store,
    profile_name,
)
from ..models import Calendar, Event, EventDraft
from ..plugin_storage import profile_dir

PLUGIN_NAME = "hermes-calendar"
TOOLSET = "calendar"
DEFAULT_SCOPE = "profile"
_TYPE = "object"
_LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"
_TIMEZONE_CONFIG_RE = re.compile(
    r"^timezone[ \t]*:[ \t]*(?P<value>[^#\n]*?)[ \t]*(?:#.*)?$", re.MULTILINE
)
_MAX_TEXT = 20_000

ToolHandler = Callable[[Mapping[str, Any]], dict[str, Any]]
_Scope = Literal["profile", "global"]
_PROFILES: tuple[_Scope, ...] = ("profile", "global")


class ToolError(Exception):
    """Base class for errors that map to a structured tool failure."""

    code = "tool_error"


class ToolArgumentError(ToolError):
    """The model supplied an argument that cannot be used."""

    code = "invalid_argument"


class ToolConflictError(ToolError):
    """The request conflicts with existing state but is otherwise valid."""

    code = "conflict"


@dataclass(frozen=True)
class _TimeValue:
    """One resolved instant plus its wall-clock representation."""

    utc: int
    local: str
    tz: str


def _json(payload: Mapping[str, Any]) -> str:
    """Serialize a tool result deterministically."""
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)


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
                raise ToolArgumentError("arguments must be a JSON object")
            payload = handler(values)
            payload.setdefault("ok", True)
            return _json(payload)
        except ToolError as exc:
            return _json({"ok": False, "error": str(exc), "code": exc.code})
        except RevisionConflict as exc:
            return _json(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "revision_conflict",
                    "event_id": exc.event_id,
                    "expected_rev": exc.expected_rev,
                }
            )
        except NotFoundError as exc:
            return _json(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "not_found",
                    "error_type": type(exc).__name__,
                }
            )
        except sqlite3.IntegrityError as exc:
            return _json(
                {
                    "ok": False,
                    "error": f"database constraint rejected the change: {exc}",
                    "code": "conflict",
                }
            )
        except DatabaseError as exc:
            return _json({"ok": False, "error": str(exc), "code": "store_error"})
        except Exception as exc:  # noqa: BLE001 - the plugin contract forbids raising into Hermes
            return _json(
                {
                    "ok": False,
                    "error": f"unexpected error: {type(exc).__name__}: {exc}",
                    "code": "internal_error",
                }
            )

    return wrapped


@contextmanager
def _repository(scope: _Scope) -> Iterator[Repository]:
    """Open exactly one store for the duration of a tool call."""
    connection = open_profile_store() if scope == "profile" else open_global_store()
    try:
        yield Repository(connection)
    finally:
        connection.close()


def _scope(args: Mapping[str, Any]) -> _Scope:
    """Validate the ``scope`` argument and default it to the profile store."""
    value = args.get("scope", DEFAULT_SCOPE)
    if value not in _PROFILES:
        raise ToolArgumentError(
            f"scope must be one of {', '.join(repr(item) for item in _PROFILES)}; got {value!r}"
        )
    return cast(_Scope, value)


def _require_str(args: Mapping[str, Any], name: str) -> str:
    """Return a required non-empty string argument."""
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"{name} is required and must be a non-empty string")
    if len(value) > _MAX_TEXT:
        raise ToolArgumentError(f"{name} must be at most {_MAX_TEXT} characters")
    return value.strip()


def _optional_str(args: Mapping[str, Any], name: str) -> str | None:
    """Return an optional string argument, rejecting other JSON types."""
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"{name} must be a string or null")
    if len(value) > _MAX_TEXT:
        raise ToolArgumentError(f"{name} must be at most {_MAX_TEXT} characters")
    return value


def _optional_bool(args: Mapping[str, Any], name: str, *, default: bool) -> bool:
    """Return an optional boolean argument."""
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ToolArgumentError(f"{name} must be a boolean")
    return value


def _require_int(args: Mapping[str, Any], name: str, *, minimum: int) -> int:
    """Return a required integer argument at or above ``minimum``."""
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"{name} is required and must be an integer")
    if value < minimum:
        raise ToolArgumentError(f"{name} must be at least {minimum}")
    return value


def _optional_int(args: Mapping[str, Any], name: str, *, minimum: int) -> int | None:
    """Return an optional integer argument at or above ``minimum``."""
    if args.get(name) is None:
        return None
    return _require_int(args, name, minimum=minimum)


def _zone(name: str, *, field: str = "timezone") -> ZoneInfo:
    """Resolve an IANA zone name or fail with a model-readable message."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolArgumentError(f"{field} must be a valid IANA zone name; got {name!r}") from exc


def _profile_home() -> Path:
    """Return the active profile home without caching it across profile switches."""
    return profile_dir(PLUGIN_NAME).parent.parent


def _configured_timezone_name() -> str | None:
    """Read ``HERMES_TIMEZONE`` or the top-level ``timezone`` key of config.yaml.

    Only the single top-level key is read. That keeps the plugin free of a YAML
    runtime dependency and matches the narrow shape Hermes itself validates.
    """
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
    """Return the profile's configured IANA zone, falling back to UTC."""
    name = _configured_timezone_name()
    if name is None:
        return "UTC"
    try:
        _zone(name)
    except ToolArgumentError:
        return "UTC"
    return name


def _validated_zone_arg(args: Mapping[str, Any], field: str) -> str | None:
    """Return a validated IANA zone argument, or ``None`` when it was omitted."""
    value = args.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"{field} must be a non-empty IANA zone name string")
    name = value.strip()
    _zone(name, field=field)
    return name


def _provided_timezone_name(args: Mapping[str, Any]) -> str | None:
    """Return a validated ``timezone`` argument, or ``None`` when it was omitted."""
    return _validated_zone_arg(args, "timezone")


def _timezone_name(args: Mapping[str, Any]) -> str:
    """Return the selected zone: explicit argument, profile setting, else UTC."""
    provided = _provided_timezone_name(args)
    return _default_timezone_name() if provided is None else provided


def _parse_time(value: Any, field: str, zone: ZoneInfo) -> _TimeValue:
    """Parse an ISO 8601 datetime into a UTC instant and local wall clock."""
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(
            f"{field} is required and must be an ISO 8601 datetime string, "
            "for example 2026-03-02T09:00:00-05:00"
        )
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolArgumentError(
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
    """Render an existing instant in ``zone``."""
    local = datetime.fromtimestamp(epoch, zone)
    return _TimeValue(utc=epoch, local=local.strftime(_LOCAL_FORMAT), tz=zone.key)


def _validate_span(start: _TimeValue, end: _TimeValue) -> None:
    """Reject empty or backwards event ranges."""
    if end.utc <= start.utc:
        raise ToolArgumentError("end must be later than start")


def _rfc3339(epoch: int) -> str:
    """Format a UTC instant as RFC 3339 with a ``Z`` suffix."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_payload(
    epoch: int, local: str | None, tz: str | None, date: str | None
) -> dict[str, Any]:
    """Canonical start/end object (``core.md`` §3.2)."""
    return {"utc": _rfc3339(epoch), "local": local, "tz": tz, "date": date}


def _calendar_payload(calendar: Calendar) -> dict[str, Any]:
    """Serialize a calendar row for a tool result."""
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
    """Serialize an event aggregate for a tool result."""
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
        "rdate": None if event.rdate is None else list(event.rdate),
        "exdate": None if event.exdate is None else list(event.exdate),
        "parent_id": event.parent_id,
        "recurrence_key": event.recurrence_key,
        "status": event.status,
        "transparency": event.transparency,
        "classification": event.classification,
        "categories": list(event.categories),
        "created_utc": event.created_utc,
        "updated_utc": event.updated_utc,
        "deleted_utc": event.deleted_utc,
    }


def _expand_events(
    repository: Repository,
    calendar_id: str,
    start_utc: int,
    end_utc: int,
) -> list[Event]:
    """RECURRENCE SEAM: the one place a future expansion engine plugs in.

    M3 deliberately returns only the stored, non-recurring rows from
    :meth:`Repository.list_events`. The recurrence milestone replaces this body
    with expansion of masters/overrides; callers and the tool contract do not
    change.
    """
    return repository.list_events(calendar_id, start_utc, end_utc)


def _scope_parameter() -> dict[str, Any]:
    """Schema fragment shared by every tool."""
    return {
        "type": "string",
        "enum": list(_PROFILES),
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
    "is interpreted in the timezone argument or the profile's timezone. Returns the "
    "stored event including its id and rev; pass rev as expected_rev to update it. "
    "All-day events, attendees, and reminders are not exposed yet.",
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
    "current event rev; a stale value returns a structured revision_conflict error "
    "and changes nothing, so re-read the event and retry. Times in the update follow "
    "the same ISO 8601 rules as creation; changing only start preserves duration.",
    {
        "event_id": {"type": "string", "description": "Event ID. Required."},
        "expected_rev": {
            "type": "integer",
            "minimum": 1,
            "description": "Revision returned by the read that this update is based on.",
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
        "scope": _scope_parameter(),
    },
    ("event_id", "expected_rev"),
)

_DELETE_EVENT_SCHEMA = _schema(
    "calendar_delete_event",
    "Soft-delete an event in the selected store. Deleting an already deleted event "
    "is a successful no-op. An optional expected_rev uses the same compare-and-swap "
    "as update and returns revision_conflict when stale.",
    {
        "event_id": {"type": "string", "description": "Event ID. Required."},
        "expected_rev": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional revision this delete is based on.",
        },
        "scope": _scope_parameter(),
    },
    ("event_id",),
)

_LIST_EVENTS_SCHEMA = _schema(
    "calendar_list_events",
    "List events overlapping a UTC instant range [start, end). Times use the same "
    "ISO 8601 rules as creation. Recurring series are NOT expanded yet and their "
    "masters are omitted; this tool returns exactly the stored non-recurring rows "
    "from the local repository (recurrence_expanded is always false). Omit "
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


@_tool
def calendar_list_calendars(args: Mapping[str, Any]) -> dict[str, Any]:
    """List calendars in the selected store."""
    scope = _scope(args)
    with _repository(scope) as repository:
        calendars = repository.list_calendars()
    return {
        "scope": scope,
        "count": len(calendars),
        "calendars": [_calendar_payload(calendar) for calendar in calendars],
    }


@_tool
def calendar_create_calendar(args: Mapping[str, Any]) -> dict[str, Any]:
    """Create a calendar in the selected store."""
    scope = _scope(args)
    name = _require_str(args, "name")
    description = _optional_str(args, "description")
    color = _optional_str(args, "color")
    read_only = _optional_bool(args, "read_only", default=False)
    provided_tz = _validated_zone_arg(args, "default_tz")
    default_tz = _default_timezone_name() if provided_tz is None else provided_tz
    with _repository(scope) as repository:
        if scope == "global" and repository.list_calendars():
            raise ToolConflictError(
                "the global store already has its single shared calendar; "
                "create additional calendars in a profile store"
            )
        calendar = repository.create_calendar(
            name,
            description=description,
            color=color,
            default_tz=default_tz,
            read_only=read_only,
        )
    return {"scope": scope, "calendar": _calendar_payload(calendar)}


@_tool
def calendar_create_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Create a timed event in an existing calendar."""
    scope = _scope(args)
    calendar_id = _require_str(args, "calendar_id")
    summary = _require_str(args, "summary")
    zone = _zone(_timezone_name(args))
    start = _parse_time(args.get("start"), "start", zone)
    end = _parse_time(args.get("end"), "end", zone)
    _validate_span(start, end)
    draft = EventDraft(
        calendar_id=calendar_id,
        summary=summary,
        description=_optional_str(args, "description"),
        location=_optional_str(args, "location"),
        url=_optional_str(args, "url"),
        start_utc=start.utc,
        end_utc=end.utc,
        start_local=start.local,
        start_tz=start.tz,
        end_local=end.local,
        end_tz=end.tz,
        duration_seconds=end.utc - start.utc,
    )
    with _repository(scope) as repository:
        event = repository.create_event(draft, actor=profile_name())
    return {"scope": scope, "event": _event_payload(event)}


@_tool
def calendar_get_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Return one non-deleted event by ID."""
    scope = _scope(args)
    event_id = _require_str(args, "event_id")
    with _repository(scope) as repository:
        event = repository.get_event(event_id)
    if event is None or event.deleted_utc is not None:
        raise EventNotFoundError(event_id)
    return {"scope": scope, "event": _event_payload(event)}


@_tool
def calendar_update_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Replace mutable event fields with compare-and-swap on ``rev``."""
    scope = _scope(args)
    event_id = _require_str(args, "event_id")
    expected_rev = _require_int(args, "expected_rev", minimum=1)
    changes: dict[str, Any] = {}
    if "summary" in args:
        summary = args["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ToolArgumentError("summary must be a non-empty string")
        if len(summary) > _MAX_TEXT:
            raise ToolArgumentError(f"summary must be at most {_MAX_TEXT} characters")
        changes["summary"] = summary.strip()
    for field in ("description", "location", "url"):
        if field in args:
            changes[field] = _optional_str(args, field)
    if not changes and "start" not in args and "end" not in args:
        raise ToolArgumentError("provide at least one field to update")
    with _repository(scope) as repository:
        current = repository.get_event(event_id)
        if current is None or current.deleted_utc is not None:
            raise EventNotFoundError(event_id)
        if "start" in args or "end" in args:
            changes.update(_updated_times(args, current))
        event = repository.update_event(
            _replace_event(current, changes),
            expected_rev=expected_rev,
            actor=profile_name(),
        )
    return {"scope": scope, "event": _event_payload(event)}


def _updated_times(args: Mapping[str, Any], current: Event) -> dict[str, Any]:
    """Resolve start/end (and the local representation) for an update."""
    provided_tz = _provided_timezone_name(args)
    if provided_tz is not None:
        zone = _zone(provided_tz)
    else:
        zone = _zone(current.start_tz or _default_timezone_name())
    if "start" in args:
        start = _parse_time(args.get("start"), "start", zone)
    else:
        start = _time_from_utc(current.start_utc, zone)
    if "end" in args:
        end = _parse_time(args.get("end"), "end", zone)
    else:
        duration = current.duration_seconds or (current.end_utc - current.start_utc)
        end = _time_from_utc(start.utc + duration, zone)
    _validate_span(start, end)
    return {
        "start_utc": start.utc,
        "start_local": start.local,
        "start_tz": start.tz,
        "end_utc": end.utc,
        "end_local": end.local,
        "end_tz": end.tz,
        "duration_seconds": end.utc - start.utc,
    }


def _replace_event(current: Event, changes: Mapping[str, Any]) -> Event:
    """Apply validated changes to a frozen event aggregate."""
    return replace(current, **changes)


@_tool
def calendar_delete_event(args: Mapping[str, Any]) -> dict[str, Any]:
    """Soft-delete an event, optionally guarded by an expected revision."""
    scope = _scope(args)
    event_id = _require_str(args, "event_id")
    expected_rev = _optional_int(args, "expected_rev", minimum=1)
    with _repository(scope) as repository:
        current = repository.get_event(event_id)
        if current is None:
            raise EventNotFoundError(event_id)
        if current.deleted_utc is not None:
            return {
                "scope": scope,
                "event_id": event_id,
                "deleted": True,
                "already_deleted": True,
            }
        repository.delete_event(event_id, expected_rev=expected_rev, actor=profile_name())
    return {
        "scope": scope,
        "event_id": event_id,
        "deleted": True,
        "already_deleted": False,
    }


@_tool
def calendar_list_events(args: Mapping[str, Any]) -> dict[str, Any]:
    """List stored non-recurring events overlapping a UTC instant range."""
    scope = _scope(args)
    zone = _zone(_timezone_name(args))
    start = _parse_time(args.get("start"), "start", zone)
    end = _parse_time(args.get("end"), "end", zone)
    _validate_span(start, end)
    requested_calendar = _optional_str(args, "calendar_id")
    with _repository(scope) as repository:
        calendars = repository.list_calendars()
        if requested_calendar is None:
            calendar_ids = [calendar.id for calendar in calendars]
        else:
            if requested_calendar not in {calendar.id for calendar in calendars}:
                raise CalendarNotFoundError(requested_calendar)
            calendar_ids = [requested_calendar]
        events: list[Event] = []
        for calendar_id in calendar_ids:
            events.extend(_expand_events(repository, calendar_id, start.utc, end.utc))
    events.sort(key=lambda event: (event.start_utc, event.id))
    return {
        "scope": scope,
        "range": {"start": start.utc, "end": end.utc, "timezone": zone.key},
        "count": len(events),
        "recurrence_expanded": False,
        "events": [_event_payload(event) for event in events],
    }


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
)

__all__ = [
    "TOOL_SPECS",
    "calendar_create_calendar",
    "calendar_create_event",
    "calendar_delete_event",
    "calendar_get_event",
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_update_event",
]
