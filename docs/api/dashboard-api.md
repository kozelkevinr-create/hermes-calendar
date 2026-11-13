# Dashboard API contract (api_version 1)

This document is the stable HTTP contract for the Hermes Calendar dashboard plugin. It
describes the JSON routes Hermes mounts under:

```text
/api/plugins/hermes-calendar/
```

The contract version is `1` and is returned by `GET /version`. Within `api_version` `1`
changes are **additive only**: new routes, new response fields, new optional request
fields, and new optional query parameters may appear; existing paths, methods, field
names, field types, status codes, and documented semantics will not change. A breaking
change bumps `api_version` and ships alongside this document. Clients must ignore unknown
response fields and must not treat a missing optional field as an error.

| Method | Path (under the prefix) | Purpose |
|---|---|---|
| GET | `/version` | Contract and plugin version. |
| GET | `/status` | Plugin, data-directory, and shared-store diagnostics. |
| GET | `/calendars` | List calendars in one store. |
| POST | `/calendar` | Create one calendar (singular path). |
| GET | `/events` | List events and expanded occurrences in a window. |
| GET | `/events/{event_id}` | Re-read one event or one expanded occurrence. |
| POST | `/events` | Create a timed, all-day, or recurring event. |
| PATCH | `/events/{event_id}` | Update a series, event, or one occurrence. |
| DELETE | `/events/{event_id}` | Soft-delete a series, event, or one occurrence. |
| POST | `/import` | Import iCalendar text idempotently by UID. |
| GET | `/export` | Export one calendar window as iCalendar text. |
| GET | `/notifications` | Page or incrementally poll profile notifications. |
| POST | `/notifications/{notification_id}/dismiss` | Acknowledge one notification. |
| POST | `/notifications/{notification_id}/snooze` | Snooze one notification. |
| GET | `/reminders/status` | Reminder-setup diagnostics. |

The machine-readable schema is [`openapi.json`](openapi.json), regenerated from the router
with `python scripts/export_openapi.py`. `python scripts/export_openapi.py --check` compares
the checked-in file's normalized contract (paths, methods, parameters, and response status
codes) and works from a plain checkout without installing the package.

## Authentication

All `/api/*` routes use the Hermes dashboard's authentication; the plugin does not
implement a second auth scheme and never sees credentials.

- **Loopback/token mode:** send `X-Hermes-Session-Token: <dashboard token>` or, for older
  bundles, `Authorization: Bearer <dashboard token>`.
- **Password-protected / gated mode:** the dashboard authenticates with its HttpOnly
  session cookie (`hermes_session_at`, plus its refresh cookie), verified by Hermes'
  `gated_auth_middleware` before the request reaches the plugin.

Auth failures are produced by Hermes, not by this plugin, so they do not use the error
envelope below. The plugin never reads `Authorization`, `Cookie`, `X-Hermes-Session-Token`,
or any other credential header, and no calendar response contains them.

## Profile selection: `?profile=<name>`

Hermes itself may serve several profiles from one dashboard and uses the `profile` query
parameter to select the profile for **any** plugin route. This is a Hermes-level routing
mechanism, not part of this plugin: it is implemented by `plugin_request_scope` in
`hermes_cli/web_server_plugin_profiles.py` on the Hermes host, which binds the request to
that profile's own plugin host and home before the route runs. Hermes consumes `profile`
from the query string; the plugin never reads it. Do not use `profile` as an
application-level parameter, and do not assume a route can override the selected profile.

## Response envelope

Successful responses are the service payload plus `"ok": true`. A failure always has
`"ok": false` and these fields:

```json
{
  "ok": false,
  "error": "summary is required",
  "code": "invalid_argument"
}
```

`code` is stable and is the field a client should branch on. `error` is human-readable and
may change. Some codes add structured fields, listed below.

### Error codes and HTTP status

| `code` | HTTP | Extra fields | Meaning |
|---|---:|---|---|
| `invalid_argument` | 400 | — | A query, path, or body value is missing, malformed, over a cap, or unknown. |
| `unsupported_recurrence` | 400 | — | The RRULE uses a part or `FREQ` outside the supported subset. `error` names every offending part. |
| `conflict` | 409 | — | The request conflicts with existing state (for example a second calendar in the global store, or a database constraint). |
| `revision_conflict` | 409 | `event_id`, `expected_rev` | `expected_rev` did not match the current master revision. See [Optimistic concurrency](#optimistic-concurrency-expected_rev). |
| `not_found` | 404 | `error_type` | The event, calendar, or notification does not exist in the selected store/profile. `error_type` is a class name such as `NotificationNotFoundError`. |
| `payload_too_large` | 413 | — | The request body, an import document, or a field exceeds its byte cap. |
| `store_error` | 503 | — | The SQLite store could not be opened or read. Details are replaced with a generic message. |
| `internal_error` | 500 | — | Unexpected failure. Details are replaced with a generic message and only the exception class is logged. |

FastAPI/Pydantic does not generate its own 422 responses here: every parameter is an
optional string validated by the plugin, so malformed input reaches the envelope above.
Routing-level 404/405 responses for a URL that matches no route come from FastAPI/Hermes,
not from this plugin.

### Example errors

REQUEST
{
  "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
  "end": "2026-03-02T10:00:00",
  "start": "2026-03-02T09:00:00"
}
HTTP 400
{
  "code": "invalid_argument",
  "error": "summary is required",
  "ok": false
}

REQUEST
{
  "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
  "end": "2026-03-02T10:00:00",
  "rrule": "FREQ=DAILY;BYHOUR=9",
  "start": "2026-03-02T09:00:00",
  "summary": "Unsupported rule",
  "timezone": "America/New_York"
}
HTTP 400
{
  "code": "unsupported_recurrence",
  "error": "unsupported RRULE part(s): BYHOUR in 'FREQ=DAILY;BYHOUR=9'",
  "ok": false
}

HTTP 409
{
  "code": "conflict",
  "error": "the global store already has its single shared calendar; create additional calendars in a profile store",
  "ok": false
}

HTTP 404
{
  "code": "not_found",
  "error": "notification rmi_missing not found",
  "error_type": "NotificationNotFoundError",
  "ok": false
}

HTTP 413
{
  "code": "payload_too_large",
  "error": "text must be at most 5242880 bytes",
  "ok": false
}

HTTP 503
{
  "code": "store_error",
  "error": "the calendar store is unavailable",
  "ok": false
}

HTTP 500
{
  "code": "internal_error",
  "error": "internal error",
  "ok": false
}

## Limits and caps

Every limit is enforced by the router, not left to the client. Going over a request body
cap returns `payload_too_large`; an over-long string or array returns `invalid_argument`.

| Limit | Value |
|---|---:|
| JSON request body (`/calendar`, `/events`, PATCH `/events/{id}`, snooze) | 262,144 bytes (256 KiB) |
| `/import` request body | 10,551,296 bytes (`2 × 5,242,880 + 65,536`) |
| `/import` `text` value | 5,242,880 bytes of UTF-8 (5 MiB) |
| Fields in a JSON body | 32; unknown fields are rejected by name |
| `scope` query/body string | 16 characters; only `profile` or `global` |
| Event/calendar identifier query or path | 128 characters |
| Notification identifier path | 128 characters |
| Time/`occurrence_start` string | 64 characters |
| Timezone name | 64 characters |
| `rrule` string | 512 characters |
| `rdates` / `exdates` values | 500 items, 64 characters each |
| Calendar name | 200 characters |
| Calendar description | 2,000 characters |
| Calendar color | 32 characters |
| Event summary | 500 characters |
| Event description | 20,000 characters (`MAX_TEXT`) |
| Event location | 2,000 characters |
| Event URL | 2,048 characters |
| Notification `limit` | default 20, minimum 1, maximum 100 |
| Notification `offset` | minimum 0, maximum 10,000 |
| Notification `state` | 32 characters, one of the visible states |
| Notification `since` | 64 characters; at most 16 digits when epoch seconds |
| ICS import parser | 5 MiB, 10,000 events, 32 nesting levels (further events are reported as warnings) |

## Time handling

- Timed events accept ISO 8601 local date-time strings plus an explicit IANA timezone via
  the `timezone` field/query: `{"start": "2026-03-02T09:00:00", "timezone":
  "America/New_York"}`. An offset in the string is also accepted and is converted to the
  requested timezone.
- When `timezone` is omitted, the plugin resolves it in this order: `HERMES_TIMEZONE`,
  the profile `config.yaml` top-level `timezone`, then `UTC`.
- Responses use UTC instants for absolute time: `start.utc` and `end.utc` are RFC 3339
  strings ending in `Z`; `start_utc` and `end_utc` are epoch seconds. `start.local` is the
  wall clock (`YYYY-MM-DDTHH:MM:SS`), `start.tz` is the IANA name, and `start.date` is
  `null` for timed events.
- All-day events use ISO dates in `start.date`/`end.date`; **the end date is exclusive**.
  `start_utc`/`end_utc` are the UTC midnights used as sort keys, while `local` and `tz` are
  `null`. A one-day event on 2026-03-20 starts `2026-03-20` and ends `2026-03-21`.
- `end` must be later than `start`; equal or inverted spans return `invalid_argument`.
- `GET /events` and `GET /export` ranges are `[start, end)` in the supplied timezone.

## Store scope: `profile` or `global`

Every calendar/event route accepts `scope`, defaulting to `profile`.

- `scope=profile` reads the signed-in profile's private store. A profile store that has
  never held a calendar is seeded with one local `Personal` calendar, so event creation
  works even in a client that cannot create calendars.
- `scope=global` reads the shared store. Hermes seed it with exactly one calendar
  (`cal_global`); additional calendars there return `conflict`.
- A response's `scope` echoes the resolved store. Scope is part of identity: the same
  event id from a different store is a different record. Notifications are profile-local,
  but a profile notification may refer to an event in the global store; its `store_id`
  says which.

## Optimistic concurrency: `expected_rev`

`PATCH /events/{event_id}` requires `expected_rev`, and `DELETE /events/{event_id}`
requires it as a query parameter. It must be an integer `>= 1` and must equal the
**master** event's current `rev` (`master_rev` for an expanded occurrence). The same token
guards whole-series and single-occurrence edits.

A stale value returns **HTTP 409** with:

```json
{
  "ok": false,
  "error": "event 01a1d0d8-0007-74df-919a-0bb3034fc203 changed: expected rev 1, retry after re-reading",
  "code": "revision_conflict",
  "event_id": "01a1d0d8-0007-74df-919a-0bb3034fc203",
  "expected_rev": 1
}
```

Recovery is read-modify-write: re-read the event or occurrence list to get the current
`rev`/`master_rev`, merge the user's intended change again, then retry with the new value.
`GET /events/{event_id}` re-reads exactly the id the 409 body returned; for an occurrence,
pass its original `occurrence_start` to get the occurrence shape and current
`master_rev`. Do not retry the same stale body blindly. The event id is returned so the
client can refetch precisely that row. A successful update increments `rev` by one.

## Recurrence

Recurring masters return an `rrule` string without the `RRULE:` prefix. `GET /events`
expands masters into occurrences and adds:

| Field | Meaning |
|---|---|
| `master_id` | Id of the series master. |
| `master_rev` | Current master `rev`; use this for `expected_rev`. |
| `master_rrule` | The master's rule, so editing an override does not clear it. |
| `is_override` | `true` when this occurrence has its own override row. |
| `override_event_id` | Override row id, or `null`. |
| `occurrence_start` / `occurrence_end` | Time object for this occurrence. |
| `occurrence_start_utc` / `occurrence_end_utc` | Epoch seconds for the occurrence. |
| `recurrence_key` | Canonical occurrence key: `T:<original start epoch>` for timed events, `D:<YYYY-MM-DD>` for all-day events. |

### Supported RRULE subset

Supported `FREQ` values: `DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`. Supported rule parts:
`FREQ`, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`, `BYSETPOS`, `WKST`.
Part names are case-insensitive and are normalized to uppercase.

Any other part (`BYHOUR`, `BYMINUTE`, `BYSECOND`, `BYWEEKNO`, `BYYEARDAY`, `BYEASTER`,
`RSCALE`, ...) or any other frequency is rejected with the structured
`unsupported_recurrence` error; it is never silently dropped. Malformed rules (missing
`FREQ`, duplicate parts, malformed `UNTIL`) return `invalid_argument`.

`RDATE` and `EXDATE` are arrays of recurrence values, at most 500 entries of 64 characters
each. Accepted spellings include ISO date/time, compact RFC 5545 forms, and the date-time
forms; the API stores and returns timed values as `YYYY-MM-DDTHH:MM:SS` (local) or
`...Z` (UTC), and all-day values as `YYYY-MM-DD`. Import reduces a `PERIOD` value to its
start with a warning.

### Single-occurrence edits and deletes: `series_scope`

- `series_scope=all` (default when `occurrence_start` is absent) edits/deletes the whole
  series. `occurrence_start` is ignored in this mode.
- `series_scope=this` targets one occurrence and requires `occurrence_start`. Supply the
  occurrence's **original** start: the `occurrence_start.local` value from `GET /events`
  for a timed occurrence, or `occurrence_start.date` for an all-day occurrence.
- Omitting `series_scope` but supplying `occurrence_start` selects `this`.
- For `this`, `rrule`, `rdates`, `exdates`, `occurrence_start`, and `series_scope` cannot
  be supplied as changes; change the series with `all` instead.
- Editing one occurrence creates or updates an override row; deleting one occurrence
  creates/updates a `CANCELLED` override. The response is that override row, with
  `parent_id`, `recurrence_key`, and `recurrence_id_raw`.
- `expected_rev` still refers to the master's `rev`, even for an override edit.
- All-day series date edits beyond creating/deleting one occurrence remain unsupported and
  return `invalid_argument`.

REQUEST
{
  "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
  "end": "2026-03-02T09:30:00",
  "exdates": [
    "2026-03-16T09:00:00"
  ],
  "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=3",
  "start": "2026-03-02T09:00:00",
  "summary": "Weekly sync",
  "timezone": "America/New_York"
}
HTTP 200
{
  "event": {
    "all_day": false,
    "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794505216,
    "deleted_utc": null,
    "description": null,
    "duration_seconds": 1800,
    "end": {
      "date": null,
      "local": "2026-03-02T09:30:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:30:00Z"
    },
    "end_utc": 1772461800,
    "exdate": [
      "2026-03-16T09:00:00"
    ],
    "floating": false,
    "id": "01a1d0d8-90d6-77c8-b842-7a2556537ab1",
    "location": null,
    "owner_profile_id": "alice",
    "parent_id": null,
    "rdate": null,
    "recurrence_id_raw": null,
    "recurrence_key": null,
    "recurrence_range": null,
    "rev": 1,
    "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=3",
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": null,
      "local": "2026-03-02T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:00:00Z"
    },
    "start_utc": 1772460000,
    "status": "CONFIRMED",
    "summary": "Weekly sync",
    "transparency": "OPAQUE",
    "uid": "01a1d0d8-90d6-76b9-ab1a-100adc87da36@hermes.local",
    "updated_utc": 1794505216,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

HTTP 200
{
  "count": 1,
  "events": [
    {
      "all_day": false,
      "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
      "categories": [],
      "classification": "PUBLIC",
      "created_utc": 1794505216,
      "deleted_utc": null,
      "description": null,
      "duration_seconds": 1800,
      "end": {
        "date": null,
        "local": "2026-03-02T09:30:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T14:30:00Z"
      },
      "end_utc": 1772461800,
      "exdate": [
        "2026-03-16T09:00:00"
      ],
      "floating": false,
      "id": "01a1d0d8-90d6-77c8-b842-7a2556537ab1",
      "is_override": false,
      "location": null,
      "master_id": "01a1d0d8-90d6-77c8-b842-7a2556537ab1",
      "master_rev": 1,
      "master_rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=3",
      "occurrence_end": {
        "date": null,
        "local": "2026-03-02T09:30:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T14:30:00Z"
      },
      "occurrence_end_utc": 1772461800,
      "occurrence_start": {
        "date": null,
        "local": "2026-03-02T09:00:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T14:00:00Z"
      },
      "occurrence_start_utc": 1772460000,
      "override_event_id": null,
      "owner_profile_id": "alice",
      "parent_id": null,
      "rdate": null,
      "recurrence_id_raw": null,
      "recurrence_key": "T:1772460000",
      "recurrence_range": null,
      "rev": 1,
      "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=3",
      "rrule_unsupported": null,
      "scope": "profile",
      "sequence": 0,
      "start": {
        "date": null,
        "local": "2026-03-02T09:00:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T14:00:00Z"
      },
      "start_utc": 1772460000,
      "status": "CONFIRMED",
      "summary": "Weekly sync",
      "transparency": "OPAQUE",
      "uid": "01a1d0d8-90d6-76b9-ab1a-100adc87da36@hermes.local",
      "updated_utc": 1794505216,
      "url": null
    }
  ],
  "ok": true,
  "range": {
    "end": 1772946000,
    "start": 1772341200,
    "timezone": "America/New_York"
  },
  "recurrence_expanded": true,
  "scope": "profile"
}

REQUEST
{
  "expected_rev": 1,
  "occurrence_start": "2026-03-02T09:00:00",
  "series_scope": "this",
  "summary": "Weekly sync (one week only)"
}
HTTP 200
{
  "event": {
    "all_day": false,
    "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794505216,
    "deleted_utc": null,
    "description": null,
    "duration_seconds": 1800,
    "end": {
      "date": null,
      "local": "2026-03-02T09:30:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:30:00Z"
    },
    "end_utc": 1772461800,
    "exdate": null,
    "floating": false,
    "id": "01a1d0d8-90ee-7a07-9b97-3d0aaa85b3f4",
    "location": null,
    "owner_profile_id": "alice",
    "parent_id": "01a1d0d8-90d6-77c8-b842-7a2556537ab1",
    "rdate": null,
    "recurrence_id_raw": "2026-03-02T09:00:00",
    "recurrence_key": "T:1772460000",
    "recurrence_range": null,
    "rev": 1,
    "rrule": null,
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": null,
      "local": "2026-03-02T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:00:00Z"
    },
    "start_utc": 1772460000,
    "status": "CONFIRMED",
    "summary": "Weekly sync (one week only)",
    "transparency": "OPAQUE",
    "uid": "01a1d0d8-90d6-76b9-ab1a-100adc87da36@hermes.local",
    "updated_utc": 1794505216,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

HTTP 200
{
  "already_deleted": false,
  "deleted": true,
  "event_id": "01a1d0d8-90d6-77c8-b842-7a2556537ab1",
  "occurrence_start": "2026-03-02T09:00:00",
  "ok": true,
  "override_event_id": "01a1d0d8-90ee-7a07-9b97-3d0aaa85b3f4",
  "recurrence_key": "T:1772460000",
  "scope": "profile"
}

## Routes

### `GET /version`

No parameters. Returns the stable contract version and the plugin version.

`plugin_version` mirrors the `version` field of the plugin's `plugin.yaml` (the repository
enforces that `plugin.yaml` and `hermes_calendar.__version__` match). It is informational;
clients gate on `api_version`.

HTTP 200
{
  "api_version": "1",
  "ok": true,
  "plugin_version": "0.0.1"
}

### `GET /status`

No parameters. Returns the same diagnostic payload as the agent's `calendar_status` tool
plus `"ok": true`: plugin name/version, profile and global data directory paths, whether
each database file exists, and the shared-store probe. Paths depend on the Hermes
installation and profile; the example uses a temporary test store.

HTTP 200
{
  "databases": {
    "global": {
      "exists": false,
      "path": "/tmp/hermes-calendar-docs-tbfspfhp/root/plugin-data/hermes-calendar/calendar-global.db"
    },
    "profile": {
      "exists": false,
      "path": "/tmp/hermes-calendar-docs-tbfspfhp/root/profiles/alice/plugin-data/hermes-calendar/calendar.db"
    }
  },
  "global_data_dir": "/tmp/hermes-calendar-docs-tbfspfhp/root/plugin-data/hermes-calendar",
  "ok": true,
  "plugin": "hermes-calendar",
  "profile_data_dir": "/tmp/hermes-calendar-docs-tbfspfhp/root/profiles/alice/plugin-data/hermes-calendar",
  "shared_store": {
    "detail": "shared store is reachable, but only this profile has registered; shared status is unknown",
    "ok": true,
    "peers_seen": [],
    "shared": null,
    "writable": true
  },
  "version": "0.0.1"
}

### `GET /calendars`

List calendars in one store.

| Query | Required | Notes |
|---|---|---|
| `scope` | no | `profile` (default) or `global`. |

Response: `scope`, `count`, and `calendars`. Each calendar carries `id`, `source_id`,
`scope`, `owner_profile_id`, `remote_id`, `name`, `description`, `color`, `default_tz`,
`read_only`, `created_utc`, and `updated_utc`.

HTTP 200
{
  "calendars": [
    {
      "color": null,
      "created_utc": 1794505100,
      "default_tz": "UTC",
      "description": null,
      "id": "cal_01a1d0d8-7c21-7b30-9e51-1f32a2c7b901",
      "name": "Personal",
      "owner_profile_id": "alice",
      "read_only": false,
      "remote_id": null,
      "scope": "profile",
      "source_id": "src_local",
      "updated_utc": 1794505100
    },
    {
      "color": "#2F6FEB",
      "created_utc": 1794505216,
      "default_tz": "America/New_York",
      "description": "Team calendar",
      "id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
      "name": "Work",
      "owner_profile_id": "alice",
      "read_only": false,
      "remote_id": null,
      "scope": "profile",
      "source_id": "src_local",
      "updated_utc": 1794505216
    }
  ],
  "count": 2,
  "ok": true,
  "scope": "profile"
}

### `POST /calendar`

Create a calendar in the selected store. The path is singular (`/calendar`), unlike the
listing route.

| Body field | Required | Limit / meaning |
|---|---|---|
| `name` | yes | 1–200 characters after trimming; non-blank. |
| `description` | no | At most 2,000 characters. |
| `color` | no | At most 32 characters, opaque to the plugin. |
| `default_tz` | no | IANA zone, at most 64 characters. Defaults through the timezone resolution order. |
| `read_only` | no | Boolean; defaults `false`. |
| `scope` | no | `profile` (default) or `global`. |

Returns `scope` and the created `calendar`. A second calendar in the global store returns
`conflict` (409).

REQUEST
{
  "color": "#2F6FEB",
  "default_tz": "America/New_York",
  "description": "Team calendar",
  "name": "Work",
  "scope": "profile"
}
HTTP 200
{
  "calendar": {
    "color": "#2F6FEB",
    "created_utc": 1794505216,
    "default_tz": "America/New_York",
    "description": "Team calendar",
    "id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
    "name": "Work",
    "owner_profile_id": "alice",
    "read_only": false,
    "remote_id": null,
    "scope": "profile",
    "source_id": "src_local",
    "updated_utc": 1794505216
  },
  "ok": true,
  "scope": "profile"
}

### `GET /events`

List non-recurring events and expanded recurring occurrences overlapping `[start, end)`.

| Query | Required | Notes |
|---|---|---|
| `start` | yes | ISO 8601 date-time (or offset form), max 64 chars. |
| `end` | yes | ISO 8601 date-time; must be later than `start`. |
| `timezone` | no | IANA zone used to interpret offset-less values. |
| `calendar_id` | no | Restrict to one calendar; unknown id returns `not_found`. Omit for every calendar in the store. |
| `scope` | no | `profile` (default) or `global`. |

Response: `scope`, `range` (`start`/`end` epoch seconds and resolved `timezone`), `count`,
`recurrence_expanded`, and `events`. `recurrence_warnings` appears only when expansion
had to degrade (for example an unsupported rule imported from ICS). Occurrences are sorted
by occurrence start, id, and recurrence key.

HTTP 200
{
  "count": 1,
  "events": [
    {
      "all_day": false,
      "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
      "categories": [],
      "classification": "PUBLIC",
      "created_utc": 1794505216,
      "deleted_utc": null,
      "description": "Bring the schema draft.",
      "duration_seconds": 3600,
      "end": {
        "date": null,
        "local": "2026-03-02T10:00:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T15:00:00Z"
      },
      "end_utc": 1772463600,
      "exdate": null,
      "floating": false,
      "id": "01a1d0d8-90b1-7b48-98a6-c044f3589baa",
      "is_override": false,
      "location": "Room 4B",
      "master_id": "01a1d0d8-90b1-7b48-98a6-c044f3589baa",
      "master_rev": 1,
      "master_rrule": null,
      "occurrence_end": {
        "date": null,
        "local": "2026-03-02T10:00:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T15:00:00Z"
      },
      "occurrence_end_utc": 1772463600,
      "occurrence_start": {
        "date": null,
        "local": "2026-03-02T09:00:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T14:00:00Z"
      },
      "occurrence_start_utc": 1772460000,
      "override_event_id": null,
      "owner_profile_id": "alice",
      "parent_id": null,
      "rdate": null,
      "recurrence_id_raw": null,
      "recurrence_key": null,
      "recurrence_range": null,
      "rev": 1,
      "rrule": null,
      "rrule_unsupported": null,
      "scope": "profile",
      "sequence": 0,
      "start": {
        "date": null,
        "local": "2026-03-02T09:00:00",
        "tz": "America/New_York",
        "utc": "2026-03-02T14:00:00Z"
      },
      "start_utc": 1772460000,
      "status": "CONFIRMED",
      "summary": "Design review",
      "transparency": "OPAQUE",
      "uid": "01a1d0d8-90b1-77ec-9168-eebd6acbb178@hermes.local",
      "updated_utc": 1794505216,
      "url": null
    }
  ],
  "ok": true,
  "range": {
    "end": 1775016000,
    "start": 1772341200,
    "timezone": "America/New_York"
  },
  "recurrence_expanded": false,
  "scope": "profile"
}

### `GET /events/{event_id}`

Re-read one event, or one occurrence of a recurring series. This is the targeted re-read
after a `PATCH`/`DELETE` returns `revision_conflict`: that 409 body already carries
`event_id`, so the client can fetch exactly that row instead of listing a window.

Path `event_id` is required and at most 128 characters. Ids are opaque strings; an over-long
value or a path segment that is only whitespace returns `invalid_argument` (HTTP 400), not
`not_found`.

| Query | Required | Notes |
|---|---|---|
| `scope` | no | `profile` (default) or `global`; must be the store that owns the event. |
| `occurrence_start` | no | One occurrence of a recurring series. Send the occurrence's **original** start: `occurrence_start.local` from `GET /events` for a timed occurrence, or `occurrence_start.date` for an all-day occurrence; max 64 characters. |

Without `occurrence_start` the response `event` is exactly the stored event shape returned
by `POST /events` and `PATCH /events/{event_id}`: `rev`, the recurrence fields, times as
`{utc, local, tz, date}`, and all-day values as dates with an exclusive end.

With `occurrence_start` the response `event` is exactly one item from `GET /events`,
including `master_id`, `master_rev` (use this as `expected_rev` on retry), `master_rrule`,
`is_override`, `override_event_id`, `occurrence_start`/`occurrence_end`,
`occurrence_start_utc`/`occurrence_end_utc`, and `recurrence_key`. A start that is not an
occurrence of the series returns `invalid_argument` (HTTP 400).

A deleted (tombstone) event and an unknown id both return `not_found` (HTTP 404). The event
must live in the selected store: a profile id read with `scope=global` (or the reverse) is
`not_found`.

GET /api/plugins/hermes-calendar/events/01a1d127-b515-7b6d-946b-1228d49d7e78
HTTP 200
{
  "event": {
    "all_day": false,
    "calendar_id": "cal_01a1d127-b504-7ff8-93fd-a3bd8cebdda6",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794510402,
    "deleted_utc": null,
    "description": "Bring the schema draft.",
    "duration_seconds": 3600,
    "end": {
      "date": null,
      "local": "2026-03-02T10:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T15:00:00Z"
    },
    "end_utc": 1772463600,
    "exdate": null,
    "floating": false,
    "id": "01a1d127-b515-7b6d-946b-1228d49d7e78",
    "location": "Room 4B",
    "owner_profile_id": "alice",
    "parent_id": null,
    "rdate": null,
    "recurrence_id_raw": null,
    "recurrence_key": null,
    "recurrence_range": null,
    "rev": 1,
    "rrule": null,
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": null,
      "local": "2026-03-02T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:00:00Z"
    },
    "start_utc": 1772460000,
    "status": "CONFIRMED",
    "summary": "Design review",
    "transparency": "OPAQUE",
    "uid": "01a1d127-b515-7be6-a282-11a08ef0c1a2@hermes.local",
    "updated_utc": 1794510402,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

GET /api/plugins/hermes-calendar/events/01a1d127-b522-77bb-bf11-cdaa43bb4781?occurrence_start=2026-03-09T09:00:00
HTTP 200
{
  "event": {
    "all_day": false,
    "calendar_id": "cal_01a1d127-b504-7ff8-93fd-a3bd8cebdda6",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794510402,
    "deleted_utc": null,
    "description": null,
    "duration_seconds": 1800,
    "end": {
      "date": null,
      "local": "2026-03-09T09:30:00",
      "tz": "America/New_York",
      "utc": "2026-03-09T13:30:00Z"
    },
    "end_utc": 1773063000,
    "exdate": null,
    "floating": false,
    "id": "01a1d127-b522-77bb-bf11-cdaa43bb4781",
    "is_override": false,
    "location": null,
    "master_id": "01a1d127-b522-77bb-bf11-cdaa43bb4781",
    "master_rev": 1,
    "master_rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=3",
    "occurrence_end": {
      "date": null,
      "local": "2026-03-09T09:30:00",
      "tz": "America/New_York",
      "utc": "2026-03-09T13:30:00Z"
    },
    "occurrence_end_utc": 1773063000,
    "occurrence_start": {
      "date": null,
      "local": "2026-03-09T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-09T13:00:00Z"
    },
    "occurrence_start_utc": 1773061200,
    "override_event_id": null,
    "owner_profile_id": "alice",
    "parent_id": null,
    "rdate": null,
    "recurrence_id_raw": null,
    "recurrence_key": "T:1773061200",
    "recurrence_range": null,
    "rev": 1,
    "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=3",
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": null,
      "local": "2026-03-09T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-09T13:00:00Z"
    },
    "start_utc": 1773061200,
    "status": "CONFIRMED",
    "summary": "Weekly sync",
    "transparency": "OPAQUE",
    "uid": "01a1d127-b522-7fa0-b893-b40a120d7450@hermes.local",
    "updated_utc": 1794510402,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

HTTP 404 (deleted tombstone or unknown id)
{
  "code": "not_found",
  "error": "01a1d127-b515-7b6d-946b-1228d49d7e78",
  "error_type": "EventNotFoundError",
  "ok": false
}

HTTP 400 (over-long or whitespace-only id, or an `occurrence_start` outside the series)
{
  "code": "invalid_argument",
  "error": "event_id must be at most 128 characters",
  "ok": false
}

This route is **additive within `api_version` 1**. Older servers do not have it and answer
the URL with a routing-level 404 that is **not** this envelope; because additive routes do
not change `api_version`, a client cannot detect support from `GET /version`. Treat a
response without the plugin envelope as "route unavailable" and fall back to `GET /events`
over a window around the event, matching `id` (or `master_id` plus `occurrence_start`). Only
an HTTP 404 whose body has `code: "not_found"` means the event was deleted.

### `POST /events`

Create a timed or all-day event, optionally recurring.

| Body field | Required | Limit / meaning |
|---|---|---|
| `calendar_id` | yes | Must exist in the selected store. |
| `summary` | yes | 1–500 characters. |
| `start` | yes | Timed: ISO date-time; all-day: ISO date. |
| `end` | yes | Timed: ISO date-time; all-day: ISO date, exclusive. Must be later than `start`. |
| `timezone` | no | IANA zone for timed values; ignored for all-day. |
| `description` | no | At most 20,000 characters. |
| `location` | no | At most 2,000 characters. |
| `url` | no | At most 2,048 characters. |
| `rrule` | no | At most 512 characters; see [Recurrence](#recurrence). |
| `rdates` | no | Array of up to 500 strings, each up to 64 characters. |
| `exdates` | no | Array of up to 500 strings, each up to 64 characters. |
| `all_day` | no | Boolean; defaults `false`. |
| `scope` | no | `profile` (default) or `global`. |

Returns `scope` and the created `event`. All-day example (note the exclusive end):

REQUEST
{
  "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
  "description": "Bring the schema draft.",
  "end": "2026-03-02T10:00:00",
  "location": "Room 4B",
  "start": "2026-03-02T09:00:00",
  "summary": "Design review",
  "timezone": "America/New_York"
}
HTTP 200
{
  "event": {
    "all_day": false,
    "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794505216,
    "deleted_utc": null,
    "description": "Bring the schema draft.",
    "duration_seconds": 3600,
    "end": {
      "date": null,
      "local": "2026-03-02T10:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T15:00:00Z"
    },
    "end_utc": 1772463600,
    "exdate": null,
    "floating": false,
    "id": "01a1d0d8-90b1-7b48-98a6-c044f3589baa",
    "location": "Room 4B",
    "owner_profile_id": "alice",
    "parent_id": null,
    "rdate": null,
    "recurrence_id_raw": null,
    "recurrence_key": null,
    "recurrence_range": null,
    "rev": 1,
    "rrule": null,
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": null,
      "local": "2026-03-02T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:00:00Z"
    },
    "start_utc": 1772460000,
    "status": "CONFIRMED",
    "summary": "Design review",
    "transparency": "OPAQUE",
    "uid": "01a1d0d8-90b1-77ec-9168-eebd6acbb178@hermes.local",
    "updated_utc": 1794505216,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

REQUEST
{
  "all_day": true,
  "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
  "end": "2026-03-21",
  "start": "2026-03-20",
  "summary": "Holiday"
}
HTTP 200
{
  "event": {
    "all_day": true,
    "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794505216,
    "deleted_utc": null,
    "description": null,
    "duration_seconds": null,
    "end": {
      "date": "2026-03-21",
      "local": null,
      "tz": null,
      "utc": "2026-03-21T00:00:00Z"
    },
    "end_utc": 1774051200,
    "exdate": null,
    "floating": false,
    "id": "01a1d0d8-90e0-752d-9e9a-1638d0407ec4",
    "location": null,
    "owner_profile_id": "alice",
    "parent_id": null,
    "rdate": null,
    "recurrence_id_raw": null,
    "recurrence_key": null,
    "recurrence_range": null,
    "rev": 1,
    "rrule": null,
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": "2026-03-20",
      "local": null,
      "tz": null,
      "utc": "2026-03-20T00:00:00Z"
    },
    "start_utc": 1773964800,
    "status": "CONFIRMED",
    "summary": "Holiday",
    "transparency": "OPAQUE",
    "uid": "01a1d0d8-90e0-7f0a-80bc-8cad7677199b@hermes.local",
    "updated_utc": 1794505216,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

### `PATCH /events/{event_id}`

Update a whole series, a non-recurring event, or one occurrence.

Path `event_id` is at most 128 characters.

| Body field | Required | Notes |
|---|---|---|
| `expected_rev` | yes | Integer `>= 1`; master revision (see above). |
| `summary` | no | 1–500 characters. An empty/blank value is rejected. |
| `description`, `location`, `url` | no | Same caps as create; `null` clears. |
| `start`, `end` | no | Same formats as create; both must remain valid. |
| `timezone` | no | IANA zone, at most 64 characters. |
| `rrule` | no | At most 512 characters; `null` clears the rule. |
| `rdates`, `exdates` | no | Arrays as above; `null` clears. |
| `occurrence_start` | no | Original occurrence start; required for `series_scope=this`. |
| `series_scope` | no | `this` or `all`; see [Single-occurrence edits](#single-occurrence-edits-and-deletes-series_scope). |
| `scope` | no | `profile` (default) or `global`. |

`all_day` is not patchable; unknown fields are rejected by name. Returns `scope` and the
updated `event` (the master, or the override for `series_scope=this`).

REQUEST
{
  "expected_rev": 1,
  "location": "Room 4C",
  "summary": "Design review (moved)"
}
HTTP 200
{
  "event": {
    "all_day": false,
    "calendar_id": "cal_01a1d0d8-90a4-714d-872d-f49906a620e2",
    "categories": [],
    "classification": "PUBLIC",
    "created_utc": 1794505216,
    "deleted_utc": null,
    "description": "Bring the schema draft.",
    "duration_seconds": 3600,
    "end": {
      "date": null,
      "local": "2026-03-02T10:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T15:00:00Z"
    },
    "end_utc": 1772463600,
    "exdate": null,
    "floating": false,
    "id": "01a1d0d8-90b1-7b48-98a6-c044f3589baa",
    "location": "Room 4C",
    "owner_profile_id": "alice",
    "parent_id": null,
    "rdate": null,
    "recurrence_id_raw": null,
    "recurrence_key": null,
    "recurrence_range": null,
    "rev": 2,
    "rrule": null,
    "rrule_unsupported": null,
    "scope": "profile",
    "sequence": 0,
    "start": {
      "date": null,
      "local": "2026-03-02T09:00:00",
      "tz": "America/New_York",
      "utc": "2026-03-02T14:00:00Z"
    },
    "start_utc": 1772460000,
    "status": "CONFIRMED",
    "summary": "Design review (moved)",
    "transparency": "OPAQUE",
    "uid": "01a1d0d8-90b1-77ec-9168-eebd6acbb178@hermes.local",
    "updated_utc": 1794505216,
    "url": null
  },
  "ok": true,
  "scope": "profile"
}

REQUEST
{
  "expected_rev": 1,
  "summary": "Stale rewrite"
}
HTTP 409
{
  "code": "revision_conflict",
  "error": "event 01a1d0d8-90b1-7b48-98a6-c044f3589baa changed: expected rev 1, retry after re-reading",
  "event_id": "01a1d0d8-90b1-7b48-98a6-c044f3589baa",
  "expected_rev": 1,
  "ok": false
}

### `DELETE /events/{event_id}`

Soft-delete a whole series/event or one occurrence. Retrying a whole-series delete is
idempotent and returns `already_deleted: true`; the row remains addressable as a tombstone.

| Query | Required | Notes |
|---|---|---|
| `expected_rev` | yes | Integer `>= 1`; master revision. |
| `scope` | no | `profile` (default) or `global`. |
| `occurrence_start` | no | Original occurrence start for `series_scope=this`. |
| `series_scope` | no | `this` or `all`; same defaulting rules as PATCH. |

Whole-series response: `scope`, `event_id`, `deleted`, `already_deleted`, and
`deleted_overrides` when live overrides were also removed. A single-occurrence delete
also returns `occurrence_start`, `recurrence_key`, and `override_event_id`.

HTTP 200
{
  "already_deleted": false,
  "deleted": true,
  "event_id": "01a1d0d8-90b1-7b48-98a6-c044f3589baa",
  "ok": true,
  "scope": "profile"
}

### `POST /import`

Import iCalendar text into one calendar. Import is idempotent by `UID`: re-importing the
same document updates masters and overrides instead of creating duplicates. It never
fetches a URL.

| Body field | Required | Notes |
|---|---|---|
| `text` | yes | Non-empty iCalendar string, at most 5 MiB of UTF-8. |
| `calendar_id` | yes | Destination calendar. |
| `scope` | no | `profile` (default) or `global`. |

Response: `scope`, `calendar_id`, `imported`, `updated`, `skipped`, and `warnings`.
Malformed calendar text is reported as warnings with zero counts rather than an error, and
parser limits (document size, 10,000 events, nesting) also surface as warnings. Missing or
oversized `text`, or an unknown calendar, returns the standard envelope.

REQUEST
{
  "calendar_id": "cal_01a1d0d8-9102-7451-b277-59cdd23ca03c",
  "scope": "profile",
  "text": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Example//EN\r\nBEGIN:VEVENT\r\nUID:import-example@example.com\r\nSUMMARY:Imported planning\r\nDTSTART:20260305T140000Z\r\nDTEND:20260305T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
}
HTTP 200
{
  "calendar_id": "cal_01a1d0d8-9102-7451-b277-59cdd23ca03c",
  "imported": 1,
  "ok": true,
  "scope": "profile",
  "skipped": 0,
  "updated": 0,
  "warnings": []
}

### `GET /export`

Export one calendar window as deterministic iCalendar text.

| Query | Required | Notes |
|---|---|---|
| `calendar_id` | yes | Calendar to export. |
| `start` | yes | Window start; inclusive. |
| `end` | yes | Window end; exclusive and later than `start`. |
| `timezone` | no | Zone used to interpret offset-less bounds. |
| `scope` | no | `profile` (default) or `global`. |

Response: `scope`, `calendar_id`, `range` (epoch bounds plus resolved timezone), `count`,
and `ics` (the complete document as a JSON string). Masters are exported with their rules
and overrides, not as expanded occurrences, so a re-import preserves series identity.

HTTP 200
{
  "calendar_id": "cal_01a1d0d8-9102-7451-b277-59cdd23ca03c",
  "count": 1,
  "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Hermes Calendar//EN\r\nCALSCALE:GREGORIAN\r\nBEGIN:VEVENT\r\nUID:import-example@example.com\r\nSUMMARY:Imported planning\r\nDTSTART:20260305T140000Z\r\nDTEND:20260305T150000Z\r\nCREATED:20261112T174016Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
  "ok": true,
  "range": {
    "end": 1775001600,
    "start": 1772323200,
    "timezone": "UTC"
  },
  "scope": "profile"
}

### `GET /notifications`

List one bounded page of the profile's notifications, newest first by `fire_at`, then id.

| Query | Required | Notes |
|---|---|---|
| `limit` | no | Default 20, 1–100. |
| `offset` | no | Default 0, 0–10,000. |
| `include_dismissed` | no | `true`/`false` (also `1/0`, `yes/no`, `on/off`); default `false`. |
| `state` | no | One of `scheduled`, `firing`, `retrying`, `delivered`, `snoozed`, `suppressed`, `failed`, `dead_letter`. |
| `since` | no | Incremental watermark: epoch seconds or ISO 8601 **with an explicit offset** (`Z` or `+02:00`). |

By default, dismissed (`acknowledged`) and cancelled notifications are hidden.
`include_dismissed=true` removes the state filter entirely, so every state is listed; use
`state` when you need one state. `total` is the unpaged count under the same filters.
Notifications are profile-local; an item referring to an event in the global store has
`store_id: "global"`.

Response fields:

| Field | Meaning |
|---|---|
| `count` | Items in this page. |
| `total` | Unpaged count matching the filters. |
| `limit`, `offset` | Echoed paging values. |
| `server_time` | Server snapshot as `{ "epoch": <int>, "iso": "<RFC 3339 UTC>" }`. |
| `notifications` | Array of notification items (below). |

Notification item fields: `id` (opaque, `rmi_`-prefixed), `title` (sanitized event summary,
or `"Calendar reminder"`), `state`, `priority`, `store_id`, `event_id`, `recurrence_key`,
`occurrence_start` (RFC 3339), `fire_at` (RFC 3339), `snooze_until` (RFC 3339 or `null`),
`attempt`, `updated_at`, and `last_delivery` (`null` or the latest delivery attempt with
`channel`, `attempt`, `outcome`, `exit_code`, `provider_id`, `error`, `latency_ms`,
`started`, `finished`). Description, location, attendees, and delivery bodies are never
included.

`updated_at` is `{ "epoch": <int>, "iso": "<RFC 3339 UTC>" }`. It advances on materialized
changes, delivery transitions, dismissal, and snooze; snooze resets `attempt` to 0.

HTTP 200
{
  "count": 2,
  "limit": 2,
  "notifications": [
    {
      "attempt": 1,
      "event_id": "01a1d0d8-9123-7674-862a-39da11023693",
      "fire_at": "2027-01-15T08:02:00Z",
      "id": "rmi_01a1d0d8-9137-73c3-aa40-b0b1c3cf6d0f",
      "last_delivery": {
        "attempt": 1,
        "channel": "in_app",
        "error": null,
        "exit_code": null,
        "finished": "2027-01-15T08:10:00Z",
        "latency_ms": 0,
        "outcome": "success",
        "provider_id": null,
        "started": "2027-01-15T08:10:00Z"
      },
      "occurrence_start": "2027-01-15T08:12:00Z",
      "priority": "normal",
      "recurrence_key": "T:1800000720",
      "snooze_until": null,
      "state": "delivered",
      "store_id": "profile:alice",
      "title": "Profile 3",
      "updated_at": {
        "epoch": 1800000600,
        "iso": "2027-01-15T08:10:00Z"
      }
    },
    {
      "attempt": 1,
      "event_id": "01a1d0d8-9120-79b5-9ab5-9bce9cd0d4f1",
      "fire_at": "2027-01-15T08:01:00Z",
      "id": "rmi_01a1d0d8-9137-7203-a209-e82e5c96b8aa",
      "last_delivery": {
        "attempt": 1,
        "channel": "in_app",
        "error": null,
        "exit_code": null,
        "finished": "2027-01-15T08:10:00Z",
        "latency_ms": 0,
        "outcome": "success",
        "provider_id": null,
        "started": "2027-01-15T08:10:00Z"
      },
      "occurrence_start": "2027-01-15T08:11:00Z",
      "priority": "normal",
      "recurrence_key": "T:1800000660",
      "snooze_until": null,
      "state": "delivered",
      "store_id": "profile:alice",
      "title": "Profile 2",
      "updated_at": {
        "epoch": 1800000600,
        "iso": "2027-01-15T08:10:00Z"
      }
    }
  ],
  "offset": 0,
  "ok": true,
  "server_time": {
    "epoch": 1794505216,
    "iso": "2026-11-12T17:40:16Z"
  },
  "total": 4
}

#### Incremental polling with `since`

`since` keeps only rows whose `updated_at` is **strictly greater** than the supplied
instant. The boundary is exclusive: `since=<the item's own updated_at epoch>` does not
return that item.

1. Poll with `since=<last watermark>` and your other filters.
2. Process the page, deduplicating by `id` (a row may be returned again if it changes
   between pages).
3. Use `server_time.epoch` from the response as the next watermark. This gives
   at-least-once delivery; do not advance the watermark from the maximum item time alone
   when a page is empty.

Invalid values (naive date-times, impossible dates, non-numeric text, more than 16 epoch
digits, or values over 64 characters) return `invalid_argument`. Epoch seconds and
offset-bearing ISO strings denoting the same instant select the same rows.

HTTP 200
{
  "count": 1,
  "limit": 1,
  "notifications": [
    {
      "attempt": 1,
      "event_id": "01a1d0d8-9123-7674-862a-39da11023693",
      "fire_at": "2027-01-15T08:02:00Z",
      "id": "rmi_01a1d0d8-9137-73c3-aa40-b0b1c3cf6d0f",
      "last_delivery": {
        "attempt": 1,
        "channel": "in_app",
        "error": null,
        "exit_code": null,
        "finished": "2027-01-15T08:10:00Z",
        "latency_ms": 0,
        "outcome": "success",
        "provider_id": null,
        "started": "2027-01-15T08:10:00Z"
      },
      "occurrence_start": "2027-01-15T08:12:00Z",
      "priority": "normal",
      "recurrence_key": "T:1800000720",
      "snooze_until": null,
      "state": "delivered",
      "store_id": "profile:alice",
      "title": "Profile 3",
      "updated_at": {
        "epoch": 1800000600,
        "iso": "2027-01-15T08:10:00Z"
      }
    }
  ],
  "offset": 0,
  "ok": true,
  "server_time": {
    "epoch": 1794505216,
    "iso": "2026-11-12T17:40:16Z"
  },
  "total": 4
}

### `POST /notifications/{notification_id}/dismiss`

Dismiss (acknowledge) one notification. No body. The id is at most 128 characters.
Repeating the call is state-idempotent: the state stays `acknowledged`, though
`updated_at` may advance. Returns `{"ok": true, "notification": {...}}`; an unknown id
returns `not_found` (404).

HTTP 200
{
  "notification": {
    "attempt": 1,
    "event_id": "01a1d0d8-9123-7674-862a-39da11023693",
    "fire_at": "2027-01-15T08:02:00Z",
    "id": "rmi_01a1d0d8-9137-73c3-aa40-b0b1c3cf6d0f",
    "last_delivery": {
      "attempt": 1,
      "channel": "in_app",
      "error": null,
      "exit_code": null,
      "finished": "2027-01-15T08:10:00Z",
      "latency_ms": 0,
      "outcome": "success",
      "provider_id": null,
      "started": "2027-01-15T08:10:00Z"
    },
    "occurrence_start": "2027-01-15T08:12:00Z",
    "priority": "normal",
    "recurrence_key": "T:1800000720",
    "snooze_until": null,
    "state": "acknowledged",
    "store_id": "profile:alice",
    "title": "Profile 3",
    "updated_at": {
      "epoch": 1794505216,
      "iso": "2026-11-12T17:40:16Z"
    }
  },
  "ok": true
}

### `POST /notifications/{notification_id}/snooze`

Snooze one notification for an allowlisted duration.

| Body field | Required | Notes |
|---|---|---|
| `minutes` | yes | Integer, exactly one of `5`, `15`, `60`, `1440`. |

Returns `{"ok": true, "notification": {...}}` with `state: "scheduled"`, the new
`snooze_until`, `attempt: 0`, and an advanced `updated_at`. Unknown or already dismissed
notifications return `not_found` (404); any other value returns `invalid_argument` (400).

REQUEST
{
  "minutes": 15
}
HTTP 200
{
  "notification": {
    "attempt": 0,
    "event_id": "01a1d0d8-9120-79b5-9ab5-9bce9cd0d4f1",
    "fire_at": "2027-01-15T08:01:00Z",
    "id": "rmi_01a1d0d8-9137-7203-a209-e82e5c96b8aa",
    "last_delivery": {
      "attempt": 1,
      "channel": "in_app",
      "error": null,
      "exit_code": null,
      "finished": "2027-01-15T08:10:00Z",
      "latency_ms": 0,
      "outcome": "success",
      "provider_id": null,
      "started": "2027-01-15T08:10:00Z"
    },
    "occurrence_start": "2027-01-15T08:11:00Z",
    "priority": "normal",
    "recurrence_key": "T:1800000660",
    "snooze_until": "2026-11-12T17:55:16Z",
    "state": "scheduled",
    "store_id": "profile:alice",
    "title": "Profile 2",
    "updated_at": {
      "epoch": 1794505216,
      "iso": "2026-11-12T17:40:16Z"
    }
  },
  "ok": true
}

### `GET /reminders/status`

No parameters. Returns the reminder-setup diagnostics used by the Reminders tab and the
`reminders doctor` command: `checks` (each with `name`, `ok`, `detail`), `last_tick` and
`last_tick_age_seconds`, `cron_appears_running`, `cron_freshness_seconds`,
`cron_command`, `tick_script`, `job_name`, `schedule`, `in_process_ticker_enabled`,
`ticker_interval_seconds`, and `delivery_target_configured`. It never reveals the delivery
target value.

The HTTP status is 200 even when the diagnostic payload's own `ok` is `false`; `ok`
reflects whether every essential setup check passed (for example an enabled external
channel with a target). Transport/store failures still use the error envelope.

HTTP 200
{
  "checks": [
    {
      "detail": "profile store opened",
      "name": "profile_store",
      "ok": true
    },
    {
      "detail": "no enabled hermes_send channel; reminders are in-app only",
      "name": "external_channel",
      "ok": false
    },
    {
      "detail": "'hermes' is available",
      "name": "hermes_binary",
      "ok": true
    },
    {
      "detail": "held by profile alice",
      "name": "global_lease",
      "ok": true
    }
  ],
  "cron_appears_running": true,
  "cron_command": "hermes cron create --name \"calendar reminders\" --no-agent --script hermes-calendar-tick.sh --deliver local --catch-up on \"1m\"",
  "cron_freshness_seconds": 180,
  "delivery_target_configured": false,
  "in_process_ticker_enabled": false,
  "job_name": "calendar reminders",
  "last_tick": "2027-01-15T08:10:00Z",
  "last_tick_age_seconds": 0,
  "ok": false,
  "schedule": "1m",
  "tick_script": "#!/bin/sh\n# One-per-profile calendar reminder tick. Keep stdout empty: cron captures it\n# and a non-empty result can duplicate the adapter's own chat message.\nset -eu\nexec python3 -m hermes_calendar reminders tick --quiet\n",
  "ticker_interval_seconds": 30
}

## Compatibility checklist for clients

- Treat ids, `uid`, `rev`, `recurrence_key`, and `updated_at` as opaque values, not as
  parseable ids (only `updated_at` is structured).
- Send `expected_rev` on every PATCH/DELETE and re-read on 409. `GET /events/{event_id}`
  reads back exactly the `event_id` from the conflict body; older servers lack that route,
  so fall back to `GET /events` when the response is not the plugin envelope.
- Send `timezone` with every timed wall-clock value; do not rely on the server's local
  zone.
- Use `since` plus `server_time` for polling, and deduplicate by notification id.
- Ignore unknown response fields; send only documented request fields, because unknown
  body fields are rejected on purpose.
- Use `api_version` from `GET /version` for compatibility, not `plugin_version`.
