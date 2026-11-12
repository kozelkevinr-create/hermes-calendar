"""FastAPI routes for the Hermes dashboard calendar tab.

Hermes mounts this router at ``/api/plugins/hermes-calendar/`` after importing
``dashboard/plugin_api.py`` from the installed plugin directory. That module is
a thin bridge; the routes and their validation live here so they use relative
imports inside :mod:`hermes_calendar` and so the agent-facing entry point
(``register``) never imports FastAPI.

Every route calls :class:`~hermes_calendar.service.CalendarService`; no calendar
policy is duplicated here. Successful responses are the service payload plus
``"ok": true``. Failures use the same codes and envelope shape as the Hermes
tools, but store and unexpected exception text is never echoed to the browser.

Input limits are deliberately explicit: body size, field count, identifier and
query lengths, recurrence-list length, and the ICS byte cap. Blocking SQLite
work runs in a worker thread so an ``in_process`` dashboard loop stays free.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..hermes.tools import error_payload
from ..ics import MAX_ICS_BYTES
from ..plugin import calendar_status
from ..reminders import service as reminder_service
from ..reminders.doctor import reminder_status
from ..service import DEFAULT_SCOPE, MAX_TEXT, CalendarService, InvalidArgument, Scope, ServiceError

__all__ = ["router"]

_LOG = logging.getLogger(__name__)

router = APIRouter()

#: The stable HTTP contract version served under ``/api/plugins/hermes-calendar``.
#: Additive changes only while this stays ``"1"``; a breaking change bumps it.
API_VERSION = "1"

MAX_JSON_BYTES = 256 * 1024
MAX_IMPORT_BODY_BYTES = MAX_ICS_BYTES * 2 + 64 * 1024
MAX_BODY_FIELDS = 32
MAX_SCOPE_LENGTH = 16
MAX_ID_LENGTH = 128
MAX_TIME_LENGTH = 64
MAX_TIMEZONE_LENGTH = 64
MAX_RRULE_LENGTH = 512
MAX_RECURRENCE_VALUE_LENGTH = 64
MAX_RECURRENCE_VALUES = 500
MAX_CALENDAR_NAME_LENGTH = 200
MAX_CALENDAR_DESCRIPTION_LENGTH = 2_000
MAX_COLOR_LENGTH = 32
MAX_SUMMARY_LENGTH = 500
MAX_LOCATION_LENGTH = 2_000
MAX_URL_LENGTH = 2_048
MAX_NOTIFICATION_ID_LENGTH = 128
MAX_NOTIFICATION_LIMIT = 100
MAX_NOTIFICATION_OFFSET = 10_000
DEFAULT_NOTIFICATION_LIMIT = 20

_SCOPES = ("profile", "global")
_PATCHABLE_INPUTS = (
    "summary",
    "description",
    "location",
    "url",
    "start",
    "end",
    "timezone",
    "rrule",
    "rdates",
    "exdates",
)
_CREATE_CALENDAR_FIELDS = frozenset(
    {"name", "description", "color", "default_tz", "read_only", "scope"}
)
_CREATE_EVENT_FIELDS = frozenset(
    {
        "calendar_id",
        "summary",
        "start",
        "end",
        "timezone",
        "description",
        "location",
        "url",
        "rrule",
        "rdates",
        "exdates",
        "all_day",
        "scope",
    }
)
_UPDATE_EVENT_FIELDS = frozenset(
    {
        "expected_rev",
        "occurrence_start",
        "series_scope",
        "scope",
        *_PATCHABLE_INPUTS,
    }
)
_IMPORT_FIELDS = frozenset({"text", "calendar_id", "scope"})
_SNOOZE_FIELDS = frozenset({"minutes"})

_HTTP_STATUS: dict[str, int] = {
    "invalid_argument": 400,
    "unsupported_recurrence": 400,
    "conflict": 409,
    "revision_conflict": 409,
    "not_found": 404,
    "payload_too_large": 413,
    "store_error": 503,
    "internal_error": 500,
}


class PayloadTooLarge(ServiceError):
    """The request body or an uploaded calendar exceeds an explicit cap."""

    code = "payload_too_large"


def _service() -> CalendarService:
    """Build the service the dashboard routes use (a test seam, too)."""
    return CalendarService()


def _error_response(exc: BaseException) -> JSONResponse:
    """Map an exception to the tool envelope with sanitized details."""
    payload = error_payload(exc, expose_details=False)
    code = str(payload.get("code", "internal_error"))
    if code in {"store_error", "internal_error"}:
        # Log the exception class only: request bodies and event text must never
        # reach the log, and a raw store error can embed SQL or row values.
        _LOG.warning("calendar dashboard request failed (%s)", type(exc).__name__)
    return JSONResponse(payload, status_code=_HTTP_STATUS.get(code, 500))


async def _run(operation: Callable[[], dict[str, Any]]) -> JSONResponse:
    """Run a blocking service call off the event loop and envelope the result."""
    try:
        payload = await asyncio.to_thread(operation)
    except Exception as exc:  # noqa: BLE001 - the HTTP boundary must not leak a traceback
        return _error_response(exc)
    result = dict(payload)
    result.setdefault("ok", True)
    return JSONResponse(result)


async def _read_json_body(
    request: Request, *, limit: int, allowed: frozenset[str]
) -> dict[str, Any]:
    """Read and validate a JSON object with explicit size and shape limits."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError as exc:
            raise InvalidArgument("Content-Length must be an integer") from exc
        if declared_bytes < 0:
            raise InvalidArgument("Content-Length must not be negative")
        if declared_bytes > limit:
            raise PayloadTooLarge(f"request body must be at most {limit} bytes")
    raw = await request.body()
    if len(raw) > limit:
        raise PayloadTooLarge(f"request body must be at most {limit} bytes")
    if not raw:
        raise InvalidArgument("request body must be a JSON object")
    try:
        decoded: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidArgument("request body must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidArgument("request body must be a JSON object")
    body = cast(dict[str, Any], decoded)
    if len(body) > MAX_BODY_FIELDS:
        raise InvalidArgument(f"request body must contain at most {MAX_BODY_FIELDS} fields")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise InvalidArgument(f"unsupported field(s): {', '.join(unknown)}")
    return body


async def _body_request(
    request: Request,
    *,
    limit: int,
    allowed: frozenset[str],
    operation: Callable[[dict[str, Any]], dict[str, Any]],
) -> JSONResponse:
    """Parse a JSON body, then run a blocking service operation."""
    try:
        body = await _read_json_body(request, limit=limit, allowed=allowed)
    except Exception as exc:  # noqa: BLE001 - validation failures are structured too
        return _error_response(exc)
    return await _run(lambda: operation(body))


def _query(value: str | None, name: str, *, max_length: int, required: bool = False) -> str | None:
    """Validate a query-string value against an explicit length limit."""
    if value is None:
        if required:
            raise InvalidArgument(f"{name} is required")
        return None
    if len(value) > max_length:
        raise InvalidArgument(f"{name} must be at most {max_length} characters")
    if required and not value.strip():
        raise InvalidArgument(f"{name} is required and must be a non-empty string")
    return value


def _query_int(
    value: str | None,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse an optional bounded integer query parameter."""
    if value is None:
        return default
    if len(value) > 16 or not value.isdigit():
        raise InvalidArgument(f"{name} must be an integer")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise InvalidArgument(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _query_bool(value: str | None, name: str, *, default: bool) -> bool:
    """Parse ``true``/``false``-style query values without Python's permissive bool()."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise InvalidArgument(f"{name} must be true or false")


def _required_query_int(value: str | None, name: str, *, minimum: int) -> int:
    """Parse a required positive integer query parameter."""
    if value is None:
        raise InvalidArgument(f"{name} is required")
    if len(value) > 16 or not value.isdigit():
        raise InvalidArgument(f"{name} must be an integer")
    parsed = int(value)
    if parsed < minimum:
        raise InvalidArgument(f"{name} must be at least {minimum}")
    return parsed


def _query_since(value: str | None) -> int | None:
    """Parse ``since`` as epoch seconds or an ISO 8601 timestamp with an offset."""
    if value is None:
        return None
    text = value.strip()
    message = "since must be epoch seconds or an ISO 8601 timestamp with an explicit UTC offset"
    if not text:
        raise InvalidArgument(message)
    if len(text) > MAX_TIME_LENGTH:
        raise InvalidArgument(f"since must be at most {MAX_TIME_LENGTH} characters")
    if text.isdigit():
        if len(text) > 16:
            raise InvalidArgument("since must be at most 16 digits of epoch seconds")
        return int(text)
    candidate = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InvalidArgument(message) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidArgument("since must include an explicit UTC offset (for example Z or +02:00)")
    return int(parsed.timestamp())


def _scope(value: str | None) -> Scope:
    """Return the selected store scope, rejecting anything else."""
    if value is None:
        return cast(Scope, DEFAULT_SCOPE)
    if value not in _SCOPES:
        raise InvalidArgument("scope must be 'profile' or 'global'")
    return cast(Scope, value)


def _str_field(
    body: dict[str, Any], name: str, *, max_length: int, required: bool = False
) -> str | None:
    """Read an optional or required string field with an explicit length cap."""
    if name not in body or body[name] is None:
        if required:
            raise InvalidArgument(f"{name} is required")
        return None
    value = body[name]
    if not isinstance(value, str):
        raise InvalidArgument(f"{name} must be a string")
    if len(value) > max_length:
        raise InvalidArgument(f"{name} must be at most {max_length} characters")
    if required and not value.strip():
        raise InvalidArgument(f"{name} is required and must be a non-empty string")
    return value


def _bool_field(body: dict[str, Any], name: str) -> bool | None:
    """Read an optional boolean field."""
    if name not in body or body[name] is None:
        return None
    value = body[name]
    if not isinstance(value, bool):
        raise InvalidArgument(f"{name} must be a boolean")
    return value


def _required_int_field(body: dict[str, Any], name: str, *, minimum: int) -> int:
    """Read a required integer field, rejecting booleans as JSON numbers."""
    if name not in body:
        raise InvalidArgument(f"{name} is required")
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(f"{name} must be an integer")
    if value < minimum:
        raise InvalidArgument(f"{name} must be at least {minimum}")
    return value


def _recurrence_field(body: dict[str, Any], name: str) -> list[str] | None:
    """Read an RDATE/EXDATE list with item and count caps."""
    if name not in body or body[name] is None:
        return None
    value = body[name]
    if not isinstance(value, list):
        raise InvalidArgument(f"{name} must be an array of strings or null")
    if len(value) > MAX_RECURRENCE_VALUES:
        raise InvalidArgument(f"{name} must contain at most {MAX_RECURRENCE_VALUES} values")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidArgument(f"{name} values must be strings")
        if not item.strip():
            raise InvalidArgument(f"{name} values must be non-empty strings")
        if len(item) > MAX_RECURRENCE_VALUE_LENGTH:
            raise InvalidArgument(
                f"{name} values must be at most {MAX_RECURRENCE_VALUE_LENGTH} characters"
            )
        values.append(item)
    return values


def _event_changes(body: dict[str, Any]) -> dict[str, Any]:
    """Build the service change map from the allowed PATCH fields."""
    changes: dict[str, Any] = {}
    if "summary" in body:
        changes["summary"] = _str_field(
            body, "summary", max_length=MAX_SUMMARY_LENGTH, required=True
        )
    for field, limit in (
        ("description", MAX_TEXT),
        ("location", MAX_LOCATION_LENGTH),
        ("url", MAX_URL_LENGTH),
    ):
        if field in body:
            changes[field] = _str_field(body, field, max_length=limit)
    for field in ("start", "end"):
        if field in body:
            changes[field] = _str_field(body, field, max_length=MAX_TIME_LENGTH, required=True)
    if "timezone" in body:
        changes["timezone"] = _str_field(body, "timezone", max_length=MAX_TIMEZONE_LENGTH)
    if "rrule" in body:
        changes["rrule"] = _str_field(body, "rrule", max_length=MAX_RRULE_LENGTH)
    for field in ("rdates", "exdates"):
        if field in body:
            changes[field] = _recurrence_field(body, field)
    return changes


def _ics_text(body: dict[str, Any]) -> str:
    """Read the imported calendar text and enforce the byte cap explicitly."""
    if "text" not in body or body["text"] is None:
        raise InvalidArgument("text is required")
    text = body["text"]
    if not isinstance(text, str):
        raise InvalidArgument("text must be a string")
    if not text.strip():
        raise InvalidArgument("text is required and must be a non-empty string")
    if len(text.encode("utf-8")) > MAX_ICS_BYTES:
        raise PayloadTooLarge(f"text must be at most {MAX_ICS_BYTES} bytes")
    return text


def _path_id(value: str, *, name: str = "event_id") -> str:
    """Validate a row identifier taken from the URL path."""
    if not value.strip():
        raise InvalidArgument(f"{name} is required")
    limit = MAX_NOTIFICATION_ID_LENGTH if name == "notification_id" else MAX_ID_LENGTH
    if len(value) > limit:
        raise InvalidArgument(f"{name} must be at most {limit} characters")
    return value


# --------------------------------------------------------------------- routes


@router.get("/calendars")
async def list_calendars(scope: str | None = None) -> JSONResponse:
    """List calendars in the profile or shared global store."""

    def operation() -> dict[str, Any]:
        return _service().list_calendars(
            scope=_scope(_query(scope, "scope", max_length=MAX_SCOPE_LENGTH))
        )

    return await _run(operation)


@router.post("/calendar")
async def create_calendar(request: Request) -> JSONResponse:
    """Create a calendar in the selected store."""

    def operation(body: dict[str, Any]) -> dict[str, Any]:
        return _service().create_calendar(
            scope=_scope(_str_field(body, "scope", max_length=MAX_SCOPE_LENGTH)),
            name=_str_field(body, "name", max_length=MAX_CALENDAR_NAME_LENGTH, required=True),
            description=_str_field(body, "description", max_length=MAX_CALENDAR_DESCRIPTION_LENGTH),
            color=_str_field(body, "color", max_length=MAX_COLOR_LENGTH),
            default_tz=_str_field(body, "default_tz", max_length=MAX_TIMEZONE_LENGTH),
            read_only=_bool_field(body, "read_only"),
        )

    return await _body_request(
        request, limit=MAX_JSON_BYTES, allowed=_CREATE_CALENDAR_FIELDS, operation=operation
    )


@router.get("/events")
async def list_events(
    start: str | None = None,
    end: str | None = None,
    scope: str | None = None,
    calendar_id: str | None = None,
    timezone: str | None = None,
) -> JSONResponse:
    """List events and expanded occurrences overlapping ``[start, end)``."""

    def operation() -> dict[str, Any]:
        return _service().list_events(
            scope=_scope(_query(scope, "scope", max_length=MAX_SCOPE_LENGTH)),
            start=_query(start, "start", max_length=MAX_TIME_LENGTH, required=True),
            end=_query(end, "end", max_length=MAX_TIME_LENGTH, required=True),
            timezone=_query(timezone, "timezone", max_length=MAX_TIMEZONE_LENGTH),
            calendar_id=_query(calendar_id, "calendar_id", max_length=MAX_ID_LENGTH),
        )

    return await _run(operation)


@router.get("/events/{event_id}")
async def get_event(
    event_id: str,
    scope: str | None = None,
    occurrence_start: str | None = None,
) -> JSONResponse:
    """Return one event, or one occurrence of a recurring series."""

    def operation() -> dict[str, Any]:
        return _service().get_event(
            scope=_scope(_query(scope, "scope", max_length=MAX_SCOPE_LENGTH)),
            event_id=_path_id(event_id),
            occurrence_start=_query(
                occurrence_start, "occurrence_start", max_length=MAX_TIME_LENGTH
            ),
        )

    return await _run(operation)


@router.post("/events")
async def create_event(request: Request) -> JSONResponse:
    """Create a timed or all-day event."""

    def operation(body: dict[str, Any]) -> dict[str, Any]:
        return _service().create_event(
            scope=_scope(_str_field(body, "scope", max_length=MAX_SCOPE_LENGTH)),
            calendar_id=_str_field(body, "calendar_id", max_length=MAX_ID_LENGTH, required=True),
            summary=_str_field(body, "summary", max_length=MAX_SUMMARY_LENGTH, required=True),
            start=_str_field(body, "start", max_length=MAX_TIME_LENGTH, required=True),
            end=_str_field(body, "end", max_length=MAX_TIME_LENGTH, required=True),
            timezone=_str_field(body, "timezone", max_length=MAX_TIMEZONE_LENGTH),
            description=_str_field(body, "description", max_length=MAX_TEXT),
            location=_str_field(body, "location", max_length=MAX_LOCATION_LENGTH),
            url=_str_field(body, "url", max_length=MAX_URL_LENGTH),
            rrule=_str_field(body, "rrule", max_length=MAX_RRULE_LENGTH),
            rdates=_recurrence_field(body, "rdates"),
            exdates=_recurrence_field(body, "exdates"),
            all_day=_bool_field(body, "all_day"),
        )

    return await _body_request(
        request, limit=MAX_JSON_BYTES, allowed=_CREATE_EVENT_FIELDS, operation=operation
    )


@router.patch("/events/{event_id}")
async def update_event(event_id: str, request: Request) -> JSONResponse:
    """Update an event or one occurrence; ``expected_rev`` is required."""

    def operation(body: dict[str, Any]) -> dict[str, Any]:
        return _service().update_event(
            scope=_scope(_str_field(body, "scope", max_length=MAX_SCOPE_LENGTH)),
            event_id=_path_id(event_id),
            expected_rev=_required_int_field(body, "expected_rev", minimum=1),
            changes=_event_changes(body),
            occurrence_start=_str_field(body, "occurrence_start", max_length=MAX_TIME_LENGTH),
            series_scope=_str_field(body, "series_scope", max_length=MAX_SCOPE_LENGTH),
        )

    return await _body_request(
        request, limit=MAX_JSON_BYTES, allowed=_UPDATE_EVENT_FIELDS, operation=operation
    )


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: str,
    expected_rev: str | None = None,
    scope: str | None = None,
    occurrence_start: str | None = None,
    series_scope: str | None = None,
) -> JSONResponse:
    """Soft-delete an event or one occurrence; ``expected_rev`` is required."""

    def operation() -> dict[str, Any]:
        return _service().delete_event(
            scope=_scope(_query(scope, "scope", max_length=MAX_SCOPE_LENGTH)),
            event_id=_path_id(event_id),
            expected_rev=_required_query_int(expected_rev, "expected_rev", minimum=1),
            occurrence_start=_query(
                occurrence_start, "occurrence_start", max_length=MAX_TIME_LENGTH
            ),
            series_scope=_query(series_scope, "series_scope", max_length=MAX_SCOPE_LENGTH),
        )

    return await _run(operation)


@router.post("/import")
async def import_ics(request: Request) -> JSONResponse:
    """Import iCalendar text into one calendar (idempotent by UID)."""

    def operation(body: dict[str, Any]) -> dict[str, Any]:
        return _service().import_ics(
            scope=_scope(_str_field(body, "scope", max_length=MAX_SCOPE_LENGTH)),
            calendar_id=_str_field(body, "calendar_id", max_length=MAX_ID_LENGTH, required=True),
            text=_ics_text(body),
        )

    return await _body_request(
        request, limit=MAX_IMPORT_BODY_BYTES, allowed=_IMPORT_FIELDS, operation=operation
    )


@router.get("/export")
async def export_ics(
    calendar_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone: str | None = None,
    scope: str | None = None,
) -> JSONResponse:
    """Export one calendar window as deterministic iCalendar text."""

    def operation() -> dict[str, Any]:
        return _service().export_ics(
            scope=_scope(_query(scope, "scope", max_length=MAX_SCOPE_LENGTH)),
            calendar_id=_query(calendar_id, "calendar_id", max_length=MAX_ID_LENGTH, required=True),
            start=_query(start, "start", max_length=MAX_TIME_LENGTH, required=True),
            end=_query(end, "end", max_length=MAX_TIME_LENGTH, required=True),
            timezone=_query(timezone, "timezone", max_length=MAX_TIMEZONE_LENGTH),
        )

    return await _run(operation)


@router.get("/notifications")
async def list_notifications(
    limit: str | None = None,
    offset: str | None = None,
    include_dismissed: str | None = None,
    state: str | None = None,
    since: str | None = None,
) -> JSONResponse:
    """List one bounded page of profile notifications, newest first.

    ``since`` enables incremental polling: it keeps only rows whose ``updated_at``
    is strictly greater than the supplied epoch second or ISO 8601 instant.
    """

    def operation() -> dict[str, Any]:
        page_limit = _query_int(
            limit,
            "limit",
            default=DEFAULT_NOTIFICATION_LIMIT,
            minimum=1,
            maximum=MAX_NOTIFICATION_LIMIT,
        )
        page_offset = _query_int(
            offset,
            "offset",
            default=0,
            minimum=0,
            maximum=MAX_NOTIFICATION_OFFSET,
        )
        include = _query_bool(include_dismissed, "include_dismissed", default=False)
        state_value = _query(state, "state", max_length=32)
        if state_value is not None and state_value not in reminder_service.VISIBLE_STATES:
            raise InvalidArgument(
                "state must be one of "
                + ", ".join(repr(item) for item in reminder_service.VISIBLE_STATES)
            )
        since_value = _query_since(since)
        with reminder_service.notification_stores() as (profile_connection, global_connection):
            return reminder_service.notification_page(
                profile_connection,
                global_connection=global_connection,
                include_dismissed=include,
                limit=page_limit,
                offset=page_offset,
                state=state_value,
                since=since_value,
            )

    return await _run(operation)


@router.post("/notifications/{notification_id}/dismiss")
async def dismiss_notification(notification_id: str) -> JSONResponse:
    """Dismiss (acknowledge) one notification; repeated dismisses are no-ops."""

    def operation() -> dict[str, Any]:
        with reminder_service.notification_stores() as (profile_connection, global_connection):
            return {
                "notification": reminder_service.dismiss_notification(
                    profile_connection,
                    notification_id=_path_id(notification_id, name="notification_id"),
                    global_connection=global_connection,
                )
            }

    return await _run(operation)


@router.post("/notifications/{notification_id}/snooze")
async def snooze_notification(notification_id: str, request: Request) -> JSONResponse:
    """Snooze one notification for an allowlisted number of minutes."""

    def operation(body: dict[str, Any]) -> dict[str, Any]:
        minutes = _required_int_field(body, "minutes", minimum=1)
        if minutes not in reminder_service.SNOOZE_MINUTES:
            allowed = ", ".join(str(item) for item in sorted(reminder_service.SNOOZE_MINUTES))
            raise InvalidArgument(f"minutes must be one of {allowed}")
        with reminder_service.notification_stores() as (profile_connection, global_connection):
            return {
                "notification": reminder_service.snooze_notification(
                    profile_connection,
                    notification_id=_path_id(notification_id, name="notification_id"),
                    minutes=minutes,
                    global_connection=global_connection,
                )
            }

    return await _body_request(
        request, limit=MAX_JSON_BYTES, allowed=_SNOOZE_FIELDS, operation=operation
    )


@router.get("/reminders/status")
async def reminders_status() -> JSONResponse:
    """Return reminder setup diagnostics without revealing the delivery target."""

    def operation() -> dict[str, Any]:
        with reminder_service.notification_stores() as (profile_connection, global_connection):
            return reminder_status(profile_connection, global_connection=global_connection)

    return await _run(operation)


@router.get("/version")
async def version() -> JSONResponse:
    """Return the stable API contract version and the plugin version."""

    def operation() -> dict[str, Any]:
        return {"api_version": API_VERSION, "plugin_version": __version__}

    return await _run(operation)


@router.get("/status")
async def status() -> JSONResponse:
    """Return the same diagnostic payload as the ``calendar_status`` tool."""

    def operation() -> dict[str, Any]:
        decoded: Any = json.loads(calendar_status({}))
        if not isinstance(decoded, dict):
            raise RuntimeError("calendar_status returned a non-object payload")
        return cast(dict[str, Any], decoded)

    return await _run(operation)
