# Hermes Calendar Plugin — Core Model Design

| | |
|---|---|
| Status | Draft v0.1 — ready for review |
| Branch | `feature/core-design` (cut from `test`) |
| Owner | Core / storage layer |
| Scope | Canonical event model, SQLite schema, recurrence expansion, change log |
| Out of scope | UI, sync transport/protocol details, invitation delivery, free-busy |
| Related | App-shell design, sync design, Hermes API research (sibling docs) |

This document is the source of truth for **what an event is** and **how it is stored and
changed**. The app shell, sync engine, and Hermes adapter all consume the model and SQL
defined here; they should not add parallel event tables.

### Decisions at a glance

- One `events` table is the aggregate root; attendees, reminders, and per-instance
  overrides are children with no independent sync identity.
- Timed events keep **both** the UTC instant and the local wall clock plus IANA zone;
  recurrence expands in local time so DST never moves a 09:00 meeting.
- All-day events are date ranges with an exclusive end date, never instants.
- Recurrence is stored as a master row (`RRULE`/`RDATE`/`EXDATE`) plus override rows
  keyed by the original occurrence start; expansion is lazy, with an optional
  disposable cache.
- A single append-only `change_log` with a global monotonic `seq`, an `origin`
  (`local`/`remote`/`system`), and soft-delete tombstones is the sync substrate.
- SQLite triggers capture every write into the log; a one-row `change_context` set
  inside each `BEGIN IMMEDIATE` transaction supplies origin/actor/batch.
- SQLite in WAL mode with exactly one writer; readers may be concurrent.
- Remote version tokens (`etag`) are opaque and preserved, never parsed.

---

## Contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Design principles](#2-design-principles)
3. [Canonical event model](#3-canonical-event-model)
4. [SQLite schema](#4-sqlite-schema)
5. [Recurrence handling](#5-recurrence-handling)
6. [Change log and sync substrate](#6-change-log-and-sync-substrate)
7. [Lifecycle, purge, and retention](#7-lifecycle-purge-and-retention)
8. [Query patterns](#8-query-patterns)
9. [Testing strategy](#9-testing-strategy)
10. [Open questions](#10-open-questions)

Appendix A — [RFC 5545 property mapping](#appendix-a--rfc-5545-property-mapping)
Appendix B — [Worked change-log example](#appendix-b--worked-change-log-example)

---

## 1. Goals and non-goals

### Goals

1. Represent a calendar event faithfully enough to round-trip through iCalendar
   (RFC 5545) without corrupting data we do not fully model.
2. Support fast "what is on my calendar between X and Y" queries for day, week, and
   month views, across local and remote calendars.
3. Expand recurring events deterministically, including DST transitions, exceptions,
   and per-instance overrides.
4. Provide a durable, ordered change log that the sync engine can push from and that
   the UI can subscribe to, with no lost or duplicated writes.
5. Run entirely on local SQLite, with a single writer, no background server required.
6. Keep the model portable: no dependency on Hermes-specific or CalDAV-specific
   concepts in the core tables; adapters map into `sources`/`calendars`/`events`.

### Non-goals

- VTODO, VJOURNAL, VFREEBUSY, VAVAILABILITY. Only VEVENT is modeled in v1.
- iTIP/iMIP scheduling: sending invitations, processing replies, attendee-side
  scheduling. Attendees are stored, not negotiated.
- Free/busy aggregation and availability search.
- Sync transport, authentication, retry policy, and remote conflict UI (owned by the
  sync design). This doc defines the data and log those components operate on.
- Attachments (`ATTACH`). Tracked as an open question.
- Full-text search in v1 (optional schema sketch is included but not required).

### Assumptions

- Exactly one process writes the SQLite database at a time; multiple readers are
  allowed (WAL). The host app owns the connection(s).
- Remote backends expose, at minimum, a stable event identifier, a version token
  (etag/revision), and calendar-level change enumeration. If a backend can only
  re-list whole collections, the sync engine synthesizes etags and the core still
  works.
- The Hermes adapter's exact capabilities are still being researched; the model
  treats them as a capability set (`sources.capabilities_json`) and degrades
  gracefully.

---

## 2. Design principles

1. **UTC for instants, wall clock for recurrence.** Every absolute point in time is
   stored as an epoch-seconds integer. Recurring events additionally store their
   original local wall time and IANA time zone, because RFC 5545 expands in local
   time and DST must not shift a 09:00 meeting to 08:00.
2. **The event is the aggregate root.** Attendees, reminders, exceptions, and
   overrides have no independent sync identity; a change to any of them bumps the
   event's `rev`. One remote resource = one event aggregate (master plus overrides).
3. **Preserve what we do not understand.** Unknown/extension iCalendar properties are
   kept in `events.xprops_json` for round-trip. Unknown RRULE parts are preserved
   verbatim and flagged rather than dropped.
4. **Derived data is disposable.** Expanded instances, series checkpoints, and
   search indexes can be deleted and rebuilt without data loss.
5. **Every write has an origin.** A write is `local`, `remote`, or `system`; the
   change log records which. Origin is what prevents remote pulls from being pushed
   straight back.
6. **The change log is append-only.** It is written in the same transaction as the
   data change, ordered by a single monotonic sequence, and never updated except to
   mark rows as pushed.
7. **Version tokens are opaque.** `etag` and remote cursors are never parsed for
   meaning, only compared.
8. **The database enforces invariants where cheap.** Checks, partial unique indexes,
   and triggers catch bugs that application code will eventually introduce.

---

## 3. Canonical event model

### 3.1 Field summary

`Req` is required in the model. Storage names are the SQLite columns from §4.

| Field | Type | Req | RFC 5545 source | Storage | Notes |
|---|---|---|---|---|---|
| `id` | opaque ID (UUID) | yes | — | `events.id` | Internal identity. Stable for the life of the row, never sent remotely. |
| `uid` | string | yes | `UID` | `events.uid` | Globally unique per calendar. Preserved exactly, including case. Master and its overrides share it. |
| `summary` | string | yes | `SUMMARY` | `events.summary` | Empty string when absent; not NULL, to keep UI simple. |
| `description` | string \| null | no | `DESCRIPTION` | `events.description` | |
| `location` | string \| null | no | `LOCATION` | `events.location` | |
| `start` | instant + tz, or date | yes | `DTSTART` | `start_utc`, `start_local`, `start_tz`, `start_date` | See §3.4. |
| `end` | instant + tz, or date | yes | `DTEND` / `DURATION` | `end_utc`, `end_local`, `end_tz`, `end_date`, `duration_seconds` | `DTEND` is exclusive. `DURATION` is normalized to seconds. |
| `tz` | IANA name \| null | cond. | `TZID` parameter | `start_tz`, `end_tz` | NULL means floating; all-day events are date-based. |
| `all_day` | bool | yes | `VALUE=DATE` | `events.is_all_day` | Date-only event. End date exclusive (§3.5). |
| `floating` | bool | yes | no `TZID`, no `Z` | `events.is_floating` | Timed event pinned to local wall time everywhere. |
| `rrule` | string \| null | no | `RRULE` | `events.rrule` | Verbatim value without the `RRULE:` prefix. |
| `rdate` / `exdate` | array \| null | no | `RDATE` / `EXDATE` | `rdate_json`, `exdate_json` | JSON arrays of RFC value strings. |
| `recurrence_id` | string \| null | cond. | `RECURRENCE-ID` | `recurrence_id_raw`, `recurrence_key` | Present only on overrides. Key is the **original** occurrence start. |
| `recurrence_range` | `THIS` \| `THISANDFUTURE` \| null | no | `RANGE` parameter | `recurrence_range` | `THISANDFUTURE` support is limited in v1 (§5.6). |
| `reminders` | array | no | `VALARM` | `event_reminders` | Relative or absolute triggers; display/audio/email actions. |
| `attendees` | array | no | `ATTENDEE` | `event_attendees` | Ordered, with role, partstat, RSVP, delegation. |
| `organizer` | object \| null | no | `ORGANIZER` | `organizer_email`, `organizer_name` | |
| `source` | object | yes | — | `events.source_id` → `sources`; `calendar_id`; `remote_href` | Denormalized source id is FK-protected (§3.8). |
| `etag` | string \| null | cond. | `getetag` (CalDAV) / revision | `events.etag` | Opaque remote version. NULL for local-only events. |
| `rev` | integer | yes | — | `events.rev` | Local revision; bumped on every aggregate mutation. Drives cache invalidation and conflict detection. |
| `sequence` | integer | yes | `SEQUENCE` | `events.sequence` | iCalendar semantic sequence. Default 0. |
| `status` | enum | yes | `STATUS` | `events.status` | `TENTATIVE` / `CONFIRMED` / `CANCELLED`. Default `CONFIRMED`. |
| `transparency` | enum | yes | `TRANSP` | `events.transparency` | `OPAQUE` / `TRANSPARENT`. Default `OPAQUE`. |
| `classification` | enum | yes | `CLASS` | `events.classification` | `PUBLIC` / `PRIVATE` / `CONFIDENTIAL`. |
| `url` | string \| null | no | `URL` | `events.url` | |
| `categories` | array | no | `CATEGORIES` | `categories_json` | JSON array of strings. |
| `created` / `updated` | instant \| null | no | `CREATED`, `LAST-MODIFIED` | `created_utc`, `last_modified_utc` | `updated_utc` is local bookkeeping; `last_modified_utc` is the remote value. |
| `dtstamp` | instant \| null | no | `DTSTAMP` | `dtstamp_utc` | |
| `deleted` | instant \| null | no | — | `events.deleted_utc` | Soft-delete tombstone; row physically purged later (§7). |
| `xprops` | object | no | any unmodeled property | `xprops_json` | Preserved for round-trip. |

### 3.2 Canonical JSON shape

This is the shape handed between core, app shell, and adapters. Adapters may not
invent fields; they map into this shape.

```json
{
  "id": "evt_01J9Z6K3Q4",
  "uid": "0f3c2b1a-9d8e-4c7b-a6f5-2e1d0c9b8a76",
  "calendarId": "cal_work",
  "source": {
    "id": "src_hermes",
    "kind": "hermes",
    "calendarId": "cal_work",
    "remoteHref": "calendars/work/events/0f3c2b1a"
  },
  "etag": "\"rev-42\"",
  "rev": 7,
  "sequence": 2,
  "summary": "Weekly design review",
  "description": "Bring the schema draft.",
  "location": "Room 4B / meet.example.com/design",
  "start": {
    "utc": "2026-03-02T14:00:00Z",
    "local": "2026-03-02T09:00:00",
    "tz": "America/New_York",
    "date": null
  },
  "end": {
    "utc": "2026-03-02T15:00:00Z",
    "local": "2026-03-02T10:00:00",
    "tz": "America/New_York",
    "date": null
  },
  "durationSeconds": 3600,
  "allDay": false,
  "floating": false,
  "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=12",
  "rdate": null,
  "exdate": ["2026-03-16T09:00:00"],
  "recurrenceId": null,
  "recurrenceRange": null,
  "parentId": null,
  "status": "CONFIRMED",
  "transparency": "OPAQUE",
  "classification": "PUBLIC",
  "organizer": { "email": "dana@example.com", "name": "Dana" },
  "attendees": [
    {
      "address": "mailto:alex@example.com",
      "email": "alex@example.com",
      "name": "Alex",
      "role": "REQ-PARTICIPANT",
      "partstat": "ACCEPTED",
      "rsvp": false,
      "cutype": "INDIVIDUAL"
    }
  ],
  "reminders": [
    {
      "action": "DISPLAY",
      "related": "START",
      "trigger": { "kind": "relative", "offsetSeconds": -600 },
      "description": "Design review in 10 minutes"
    }
  ],
  "categories": ["work", "design"],
  "url": null,
  "createdUtc": 1767225600,
  "updatedUtc": 1772467200,
  "lastModifiedUtc": 1772467200,
  "dtstampUtc": 1772467200,
  "deletedUtc": null,
  "xprops": {}
}
```

Serialization rules:

- Instants are RFC 3339 UTC strings at the API boundary; epoch seconds in SQLite.
- Local wall times are `YYYY-MM-DDTHH:MM:SS` with no offset.
- All-day dates are `YYYY-MM-DD`; the end date is **exclusive**.
- Absent optional values are `null`, except `summary` (empty string) and `rev`
  (integer).
- Arrays preserve RFC order where order is meaningful (attendees, categories).

### 3.3 Identity and uniqueness

| Identifier | Scope | Mutability | Purpose |
|---|---|---|---|
| `events.id` | database | never | Local joins, overrides, cache keys. |
| `events.uid` | calendar | stable; changes only if remote changes it | RFC identity and remote matching. |
| `(calendar_id, uid)` for master | calendar | unique | One master per UID per calendar. |
| `(parent_id, recurrence_key)` | series | unique | One override per occurrence. |
| `remote_href` | calendar | stable in practice | Remote resource locator. Master and overrides share it. |
| `etag` | event | every remote change | Optimistic concurrency. |
| `rev` | event | every local/remote change | Cache invalidation and dirty detection. |

Rules:

- A master event has `parent_id IS NULL` and `recurrence_key IS NULL`.
- An override has both `parent_id` and `recurrence_key` set, plus `uid` equal to its
  master's `uid` and the same `calendar_id`. Enforced by trigger
  (`trg_events_override_guard`).
- `recurrence_key` is canonical and time-zone independent:
  - timed: `"T:" || start_utc` where `start_utc` is the **original** occurrence
    instant from the master's expansion;
  - all-day: `"D:" || YYYY-MM-DD`.
- Moving an event between calendars is a delete+create from the source calendar's
  perspective. The core keeps the same `events.id` and `uid` only if the remote
  supports move; otherwise it tombstones and re-creates. The sync design decides
  which path a given adapter uses.
- Duplicate `UID`s inside one remote collection are a protocol violation. Importers
  keep the first master and log a warning; overrides are still accepted.

### 3.4 Time model

| Concept | Representation | Example |
|---|---|---|
| Absolute instant | epoch seconds, UTC | `1772467200` |
| Local wall time | text, no offset | `2026-03-02T09:00:00` |
| All-day date | text date | `2026-03-02` |
| Time zone | IANA name | `America/New_York` |
| Fixed-offset zone | IANA `Etc/GMT±N` or custom VTIMEZONE | `Etc/GMT+5` |

Rules:

1. **Timed events** store `start_local` + `start_tz` + resolved `start_utc`, and the
   same triple for the end where an explicit `DTEND` exists. `duration_seconds` is
   always `end_utc - start_utc`; a `DURATION` property is converted using its nominal
   length in seconds.
2. **Recurrence expands in local wall time.** A weekly 09:00
   `America/New_York` event stays at 09:00 local after DST; the UTC instants shift.
   This is only possible because the local time and zone are retained.
3. **Floating events** (`is_floating = 1`) have `start_tz = NULL`. They represent
   "09:00 wherever the user is". For indexing and sorting, `start_utc` is resolved at
   write time using the calendar's `default_tz`; it is a display key, not a semantic
   instant. Changing the calendar's default zone does not rewrite existing rows.
4. **All-day events** (`is_all_day = 1`) are date ranges, independent of time zone.
   `start_utc`/`end_utc` are set to UTC midnight of `start_date`/`end_date` purely as
   stable sort keys. Semantics come from the date columns; range queries use dates.
5. **DST resolution** follows RFC 5545 §3.3.5:
   - ambiguous local time (fall back): use the **first** occurrence, i.e. the offset
     in effect before the transition;
   - nonexistent local time (spring forward): interpret using the UTC offset
     **before** the gap, which lands after the transition (e.g. 02:30 becomes 03:30
     local).
   The expansion engine and importer must share one implementation of this rule.
6. **Custom VTIMEZONE definitions** (TZID not present in the IANA database) are
   stored in `custom_timezones` and take precedence over the calendar default for
   that TZID. If a definition is missing, expansion uses the IANA database and emits
   a warning; it never silently substitutes UTC.
7. **Time zone database updates** can change future occurrence instants for historical
   or newly legislated rules. `series_state.tzdb_version` records which version was
   used; a version change invalidates derived instances (§5.9).

### 3.5 All-day semantics

- `DTSTART;VALUE=DATE:20260302` and `DTEND;VALUE=DATE:20260304` → `start_date =
  2026-03-02`, `end_date = 2026-03-04`, visible on March 2 and 3 (end exclusive,
  per RFC 5545 §3.8.2.2).
- A single-day event has `end_date = start_date + 1 day`.
- `DURATION:P1D` on an all-day event is converted to an exclusive end date.
- UI layers format the date range in the **viewer's** zone; the database never
  shifts all-day dates.
- Reminders on all-day events are relative to local midnight in the event's
  calendar zone, unless the remote provides an absolute trigger.

### 3.6 Recurrence representation

- The master row stores `rrule`, `rdate_json`, `exdate_json` and the original
  `DTSTART`. It is the only row with a non-NULL `rrule`.
- Each modified or cancelled instance is a separate row with `parent_id` pointing at
  the master and `recurrence_key` identifying which occurrence it replaces.
- A cancelled instance is an override row with `status = 'CANCELLED'`. It is
  equivalent to an `EXDATE` for display but carries metadata (who cancelled, when)
  and is preferred when a remote sends a `STATUS:CANCELLED` override.
- `EXDATE` entries stay on the master; they are not rows. This matches iCalendar and
  keeps imports idempotent.
- `RDATE` entries are stored as canonical RFC value strings in `rdate_json`.
  `PERIOD` values are reduced to their start instant (v1), with a warning.

### 3.7 Reminders

`VALARM` is modeled as a child list. Supported v1 subset:

| RFC concept | Support |
|---|---|
| `ACTION:DISPLAY`, `AUDIO`, `EMAIL` | Stored. Delivery of EMAIL/AUDIO is host-app responsibility. |
| `TRIGGER` relative (`-PT10M`) | Full. Stored as negative offset seconds. |
| `TRIGGER` absolute (`VALUE=DATE-TIME`) | Full. Stored as UTC instant. |
| `RELATED=START` / `RELATED=END` | Full. |
| `REPEAT` + `DURATION` | Stored as `repeat_count` + `repeat_interval_seconds`. |
| `ACKNOWLEDGED` | Not stored in v1; per-user delivery state belongs to the app. |

Multiple identical reminders on one event are deduplicated by the unique constraint.
Reminder delivery state (snooze, dismiss) is intentionally **not** core data; it is
per-device/per-user state and is an open question (Q9).

### 3.8 Source and etag

`source` is an object in the API and a pair of columns in storage:

```text
source = { id, kind: 'local' | 'caldav' | 'hermes' | 'ics',
           calendarId, remoteHref }
```

- `events.source_id` is denormalized from `calendars.source_id` for cheap filtering,
  but a composite foreign key `(calendar_id, source_id)` → `calendars(id, source_id)`
  makes inconsistency impossible.
- `source.kind` is authoritative for adapter behavior. Adding a kind is a schema
  migration only because of the `CHECK` constraint.
- `etag` is the remote version of the event resource. It is `NULL` for local events.
  It is compared, never parsed, and preserved even when the event body is otherwise
  unchanged so a subsequent push can use `If-Match`.
- `rev` is local and monotonic. Every mutation batch must increment it **at least
  once** (child-table triggers add one bump per child row, so an event plus three
  attendees ends at `rev = 4`). The change-log triggers are keyed on `rev` changes;
  a mutation without any `rev` bump is a bug and is caught by tests (§9). `rev` is
  an opaque revision counter, not a change count: only equality matters to readers.
- `calendars.sync_token` / `calendars.ctag` are the remote collection cursor and
  collection version; their interpretation belongs to the sync engine.

---

## 4. SQLite schema

Target: SQLite **3.38+** (uses `unixepoch()`, JSON1, partial indexes, strict-ish
checks). Every DDL block in this document has been executed against SQLite 3.45 as a
whole; CI should keep doing exactly that so the document cannot rot. Schema version is
tracked both in `schema_meta` and `PRAGMA user_version`.

### 4.1 Connection pragmas

Applied on every connection open:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
PRAGMA user_version = 1;
```

Write transactions use `BEGIN IMMEDIATE` so the write lock is acquired before any
reads, avoiding `SQLITE_BUSY` upgrade failures and making sequence assignment
serialized (§6.3).

### 4.2 Metadata and sources

```sql
CREATE TABLE schema_meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO schema_meta (key, value) VALUES
  ('schema_version', '1'),
  ('created_by', 'hermes-calendar'),
  ('min_available_seq', '1');

CREATE TABLE sources (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL
                    CHECK (kind IN ('local', 'caldav', 'hermes', 'ics')),
  display_name      TEXT NOT NULL,
  config_json       TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(config_json)),
  capabilities_json TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(capabilities_json)),
  created_utc       INTEGER NOT NULL,
  updated_utc       INTEGER NOT NULL,
  deleted_utc       INTEGER,
  CHECK (deleted_utc IS NULL OR deleted_utc >= created_utc)
);

CREATE TABLE calendars (
  id            TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  remote_id     TEXT,
  name          TEXT NOT NULL,
  description   TEXT,
  color         TEXT,
  default_tz    TEXT NOT NULL DEFAULT 'UTC',
  read_only     INTEGER NOT NULL DEFAULT 0 CHECK (read_only IN (0, 1)),
  sync_token    TEXT,
  ctag          TEXT,
  created_utc   INTEGER NOT NULL,
  updated_utc   INTEGER NOT NULL,
  deleted_utc   INTEGER,
  UNIQUE (id, source_id),
  UNIQUE (source_id, remote_id)
);

CREATE TABLE custom_timezones (
  tzid          TEXT PRIMARY KEY,
  source_id     TEXT REFERENCES sources(id) ON DELETE CASCADE,
  vtimezone_ics TEXT NOT NULL,
  updated_utc   INTEGER NOT NULL
) WITHOUT ROWID;
```

`UNIQUE (id, source_id)` exists solely to be the parent key of the composite foreign
key on `events`. SQLite requires a unique index whose columns match the FK target
exactly.

### 4.3 Events

```sql
CREATE TABLE events (
  id                TEXT PRIMARY KEY,
  calendar_id       TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  uid               TEXT NOT NULL,
  remote_href       TEXT,
  etag              TEXT,
  rev               INTEGER NOT NULL DEFAULT 1 CHECK (rev > 0),
  sequence          INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),

  summary           TEXT NOT NULL DEFAULT '',
  description       TEXT,
  location          TEXT,
  url               TEXT,

  -- Timed representation: wall clock + zone + resolved instant.
  start_local       TEXT,
  start_tz          TEXT,
  start_utc         INTEGER,
  end_local         TEXT,
  end_tz            TEXT,
  end_utc           INTEGER,
  duration_seconds  INTEGER,

  -- Date representation (all-day).
  start_date        TEXT,
  end_date          TEXT,

  is_all_day        INTEGER NOT NULL DEFAULT 0 CHECK (is_all_day IN (0, 1)),
  is_floating       INTEGER NOT NULL DEFAULT 0 CHECK (is_floating IN (0, 1)),

  -- Recurrence.
  rrule             TEXT,
  rdate_json        TEXT CHECK (rdate_json IS NULL OR json_valid(rdate_json)),
  exdate_json       TEXT CHECK (exdate_json IS NULL OR json_valid(exdate_json)),
  parent_id         TEXT REFERENCES events(id) ON DELETE CASCADE,
  recurrence_key    TEXT,
  recurrence_id_raw TEXT,
  recurrence_range  TEXT
                    CHECK (recurrence_range IN ('THIS', 'THISANDFUTURE')),

  status            TEXT NOT NULL DEFAULT 'CONFIRMED'
                    CHECK (status IN ('TENTATIVE', 'CONFIRMED', 'CANCELLED')),
  transparency      TEXT NOT NULL DEFAULT 'OPAQUE'
                    CHECK (transparency IN ('OPAQUE', 'TRANSPARENT')),
  classification    TEXT NOT NULL DEFAULT 'PUBLIC'
                    CHECK (classification IN ('PUBLIC', 'PRIVATE', 'CONFIDENTIAL')),

  organizer_email   TEXT,
  organizer_name    TEXT,
  categories_json   TEXT CHECK (categories_json IS NULL
                                OR json_valid(categories_json)),
  xprops_json       TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(xprops_json)),

  created_utc       INTEGER,
  updated_utc       INTEGER NOT NULL,
  dtstamp_utc       INTEGER,
  last_modified_utc INTEGER,
  deleted_utc       INTEGER,

  FOREIGN KEY (calendar_id, source_id)
    REFERENCES calendars (id, source_id)
    ON UPDATE CASCADE ON DELETE CASCADE,

  -- Overrides are the only rows with a recurrence key; masters never have one.
  CHECK ((parent_id IS NULL AND recurrence_key IS NULL)
      OR (parent_id IS NOT NULL AND recurrence_key IS NOT NULL)),

  -- Overrides never carry their own RRULE; they replace one occurrence.
  CHECK (parent_id IS NULL OR rrule IS NULL),

  -- A master's UID must not be empty; overrides inherit it.
  CHECK (length(trim(uid)) > 0),

  -- Exactly one of the timed/date representations is populated and coherent.
  CHECK (
    (is_all_day = 1
      AND start_date IS NOT NULL
      AND end_date IS NOT NULL
      AND end_date > start_date
      AND start_local IS NULL AND start_tz IS NULL
      AND end_local IS NULL AND end_tz IS NULL
      AND duration_seconds IS NULL)
    OR
    (is_all_day = 0
      AND start_local IS NOT NULL
      AND start_utc IS NOT NULL
      AND end_utc IS NOT NULL
      AND duration_seconds IS NOT NULL
      AND duration_seconds >= 0
      AND end_utc - start_utc = duration_seconds
      AND start_date IS NULL AND end_date IS NULL)
  ),

  -- Floating events have no zone; zoned events must have one.
  CHECK (is_all_day = 1
      OR (is_floating = 1 AND start_tz IS NULL)
      OR (is_floating = 0 AND start_tz IS NOT NULL)),

  CHECK (rrule IS NULL OR length(trim(rrule)) > 0),
  CHECK (recurrence_range IS NULL OR parent_id IS NOT NULL)
);
```

Notes:

- `end_local`/`end_tz`/`end_utc` are populated for every timed event when the source
  supplied `DTEND` (or it can be derived). `end_utc` and `duration_seconds` are
  always populated and drive queries and recurrence math; `end_local`/`end_tz`
  preserve the original wall-clock end for round-trip.
- `end_utc - start_utc = duration_seconds` is intentionally strict. It defines
  duration as an exact instant delta, which is what recurrence expansion needs.
  Nominal-day durations across DST are converted once at import (§5.4).
- `rev` is the change token. `updated_utc` is display bookkeeping.
- `deleted_utc` is the local tombstone time; it is not the remote `LAST-MODIFIED`
  and is allowed to differ from it.

### 4.4 Attendees and reminders

```sql
CREATE TABLE event_attendees (
  event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL CHECK (ordinal >= 0),
  address         TEXT NOT NULL,           -- raw CAL-ADDRESS, e.g. mailto:a@b.c
  email           TEXT,                    -- normalized lowercase, no mailto:
  name            TEXT,
  role            TEXT NOT NULL DEFAULT 'REQ-PARTICIPANT'
                  CHECK (role IN ('REQ-PARTICIPANT', 'OPT-PARTICIPANT',
                                  'NON-PARTICIPANT', 'CHAIR')),
  partstat        TEXT NOT NULL DEFAULT 'NEEDS-ACTION'
                  CHECK (partstat IN ('NEEDS-ACTION', 'ACCEPTED', 'DECLINED',
                                      'TENTATIVE', 'DELEGATED')),
  rsvp            INTEGER NOT NULL DEFAULT 0 CHECK (rsvp IN (0, 1)),
  cutype          TEXT CHECK (cutype IN ('INDIVIDUAL', 'GROUP', 'RESOURCE',
                                         'ROOM', 'UNKNOWN')),
  delegated_to    TEXT,
  delegated_from  TEXT,
  sent_by         TEXT,
  PRIMARY KEY (event_id, ordinal)
) WITHOUT ROWID;

CREATE TABLE event_reminders (
  id                       TEXT PRIMARY KEY,
  event_id                 TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  action                   TEXT NOT NULL DEFAULT 'DISPLAY'
                           CHECK (action IN ('DISPLAY', 'AUDIO', 'EMAIL')),
  related                  TEXT NOT NULL DEFAULT 'START'
                           CHECK (related IN ('START', 'END')),
  trigger_kind             TEXT NOT NULL
                           CHECK (trigger_kind IN ('RELATIVE', 'ABSOLUTE')),
  offset_seconds           INTEGER,        -- negative = before
  absolute_utc             INTEGER,
  repeat_count             INTEGER NOT NULL DEFAULT 0 CHECK (repeat_count >= 0),
  repeat_interval_seconds  INTEGER CHECK (repeat_interval_seconds IS NULL
                                          OR repeat_interval_seconds > 0),
  description              TEXT,
  CHECK (
    (trigger_kind = 'RELATIVE'
      AND offset_seconds IS NOT NULL AND absolute_utc IS NULL)
    OR
    (trigger_kind = 'ABSOLUTE'
      AND absolute_utc IS NOT NULL AND offset_seconds IS NULL)
  ),
  CHECK (repeat_count = 0 OR repeat_interval_seconds IS NOT NULL)
);

-- NULLs are distinct in a UNIQUE constraint, so dedupe explicitly.
CREATE UNIQUE INDEX ux_reminders_dedupe ON event_reminders(
  event_id, action, related, trigger_kind,
  COALESCE(offset_seconds, 0), COALESCE(absolute_utc, 0)
);

CREATE INDEX idx_attendees_email ON event_attendees(email)
  WHERE email IS NOT NULL;
CREATE INDEX idx_reminders_event ON event_reminders(event_id);
```

Attendee `email` is derived from `address` by stripping `mailto:` and lowercasing.
Non-email calendar addresses (e.g. `urn:uuid:...`) keep `email = NULL` and remain
searchable by `address`.

### 4.5 Derived recurrence cache (disposable)

The v1 implementation may expand on read and ignore these tables. They are specified
because they become necessary for reminders and large calendars, and because their
invalidation rules are subtle enough to design once.

```sql
CREATE TABLE event_instances (
  event_id          TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  recurrence_key    TEXT NOT NULL,
  start_utc         INTEGER NOT NULL,
  end_utc           INTEGER NOT NULL,
  start_date        TEXT,
  end_date          TEXT,
  is_all_day        INTEGER NOT NULL DEFAULT 0 CHECK (is_all_day IN (0, 1)),
  is_override       INTEGER NOT NULL DEFAULT 0 CHECK (is_override IN (0, 1)),
  override_event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
  master_rev        INTEGER NOT NULL,
  expanded_utc      INTEGER NOT NULL,
  PRIMARY KEY (event_id, recurrence_key)
) WITHOUT ROWID;

CREATE INDEX idx_instances_range ON event_instances(start_utc, end_utc);
CREATE INDEX idx_instances_dates ON event_instances(start_date, end_date)
  WHERE is_all_day = 1;

CREATE TABLE series_state (
  event_id             TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  master_rev           INTEGER NOT NULL,
  rrule_hash           TEXT NOT NULL,
  expanded_from_utc    INTEGER,
  expanded_through_utc INTEGER,
  generated_count      INTEGER NOT NULL DEFAULT 0,
  tzdb_version         TEXT,
  updated_utc          INTEGER NOT NULL
) WITHOUT ROWID;
```

Invalidation is automatic (triggers, §4.8): any `rev` change on a master or on one of
its overrides deletes the affected cache rows inside the same transaction. Readers
must also verify `master_rev = events.rev` before trusting an instance row.

### 4.6 Change log, batches, cursors, conflicts

```sql
CREATE TABLE change_context (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  origin   TEXT NOT NULL DEFAULT 'local'
           CHECK (origin IN ('local', 'remote', 'system')),
  actor    TEXT,
  batch_id TEXT,
  suppress INTEGER NOT NULL DEFAULT 0 CHECK (suppress IN (0, 1))
);

INSERT INTO change_context (id) VALUES (1);

CREATE TABLE change_log (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc         INTEGER NOT NULL,
  entity_type    TEXT NOT NULL
                 CHECK (entity_type IN ('source', 'calendar', 'event')),
  entity_id      TEXT NOT NULL,
  calendar_id    TEXT,
  op             TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
  origin         TEXT NOT NULL CHECK (origin IN ('local', 'remote', 'system')),
  actor          TEXT,
  batch_id       TEXT,
  rev            INTEGER,
  before_json    TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
  after_json     TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
  pushed_utc     INTEGER,
  push_error     TEXT,
  CHECK (before_json IS NOT NULL OR after_json IS NOT NULL),
  CHECK (origin <> 'system' OR pushed_utc IS NOT NULL)
);

CREATE INDEX idx_change_entity  ON change_log(entity_type, entity_id, seq);
CREATE INDEX idx_change_calendar ON change_log(calendar_id, seq);
CREATE INDEX idx_change_unpushed ON change_log(seq)
  WHERE origin = 'local' AND pushed_utc IS NULL;

CREATE TABLE sync_batches (
  id            TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  direction     TEXT NOT NULL CHECK (direction IN ('pull', 'push')),
  started_utc   INTEGER NOT NULL,
  finished_utc  INTEGER,
  status        TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'ok', 'partial', 'failed')),
  cursor_before TEXT,
  cursor_after  TEXT,
  stats_json    TEXT CHECK (stats_json IS NULL OR json_valid(stats_json))
);

CREATE TABLE sync_cursors (
  client_id   TEXT PRIMARY KEY,
  cursor_seq  INTEGER NOT NULL,
  updated_utc INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE sync_conflicts (
  id           TEXT PRIMARY KEY,
  entity_type  TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  source_id    TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  detected_utc INTEGER NOT NULL,
  local_json   TEXT CHECK (local_json IS NULL OR json_valid(local_json)),
  remote_json  TEXT CHECK (remote_json IS NULL OR json_valid(remote_json)),
  resolution   TEXT NOT NULL DEFAULT 'pending'
               CHECK (resolution IN ('pending', 'local', 'remote', 'merged')),
  resolved_utc INTEGER
);
```

Semantics:

- `change_log` is append-only. Only `pushed_utc` and `push_error` may be updated.
- `seq` is global and monotonic. With a single writer and `AUTOINCREMENT`, commit
  order equals sequence order; a reader can treat `seq` as a total order.
- `origin = 'system'` rows (migrations, maintenance) are never pushed. Triggers mark
  them pushed automatically, and the CHECK refuses a hand-written system row without
  `pushed_utc`, so they cannot appear in `pending_push`.
- `change_context.origin` defaults to `'local'`. That is the safe failure mode: if a
  writer forgets to set the context, a change is (harmlessly) pushed to the remote
  rather than silently withheld. The repository must still set it on every
  transaction (§6.3).
- `delete` rows keep the last known state in `before_json` so a pusher can still
  address the remote resource (UID, href, etag) after the local row is tombstoned
  or purged.
- `op = 'insert'` and `op = 'update'` are both upserts for consumers. Resurrecting a
  tombstoned event logs `update`. Consumers must not assume a strict state machine.
- `before_json`/`after_json` are **row snapshots**, not full aggregates. They carry
  identity and version fields plus the columns that commonly matter. A consumer that
  needs the full event re-reads it by `entity_id`; if the row is gone, the change is
  a deletion. This keeps triggers small and avoids duplicating attendee/reminder JSON
  on every child edit.
- `sync_conflicts` is written by the sync engine (owned by sync design); the core
  provides the table because it must be transactional with the event write.

### 4.7 Indexes and views

```sql
-- Non-recurring, non-deleted events by instant range: the common month view.
CREATE INDEX idx_events_range ON events(start_utc, end_utc)
  WHERE deleted_utc IS NULL AND parent_id IS NULL AND rrule IS NULL;

-- All-day range queries.
CREATE INDEX idx_events_dates ON events(start_date, end_date)
  WHERE deleted_utc IS NULL AND is_all_day = 1 AND parent_id IS NULL;

-- Recurring masters that may intersect a window.
CREATE INDEX idx_events_series ON events(calendar_id, start_utc)
  WHERE deleted_utc IS NULL AND parent_id IS NULL AND rrule IS NOT NULL;

CREATE INDEX idx_events_calendar_start ON events(calendar_id, start_utc)
  WHERE deleted_utc IS NULL;

CREATE INDEX idx_events_uid ON events(uid);
CREATE INDEX idx_events_href ON events(calendar_id, remote_href)
  WHERE remote_href IS NOT NULL;
CREATE INDEX idx_events_parent ON events(parent_id)
  WHERE parent_id IS NOT NULL;
CREATE INDEX idx_events_updated ON events(updated_utc);
CREATE INDEX idx_events_tombstones ON events(deleted_utc)
  WHERE deleted_utc IS NOT NULL;

CREATE UNIQUE INDEX ux_events_uid_master
  ON events(calendar_id, uid)
  WHERE parent_id IS NULL;

CREATE UNIQUE INDEX ux_events_override
  ON events(parent_id, recurrence_key)
  WHERE parent_id IS NOT NULL;

CREATE VIEW event_view AS
SELECT
  e.*,
  c.name          AS calendar_name,
  c.default_tz    AS calendar_tz,
  c.read_only     AS calendar_read_only,
  s.kind          AS source_kind,
  s.display_name  AS source_name
FROM events e
JOIN calendars c ON c.id = e.calendar_id
JOIN sources s   ON s.id = e.source_id;

-- One row per entity that has unpushed local changes, latest sequence first.
CREATE VIEW pending_push AS
SELECT l.entity_type, l.entity_id, MAX(l.seq) AS seq
FROM change_log l
WHERE l.origin = 'local' AND l.pushed_utc IS NULL
GROUP BY l.entity_type, l.entity_id;
```

### 4.8 Triggers

Triggers are the **normative** change-capture mechanism. Application code must not
write `change_log` directly (except to mark rows pushed). This guarantees that no
write path, including a future migration script, silently misses the log. Writers set
`change_context` as the first statement inside the same `BEGIN IMMEDIATE`
transaction; because SQLite has one writer, the singleton context cannot interleave
between connections.

Trigger contract:

1. Any content change to `events` increments `rev` and `updated_utc`.
2. Any change to attendee/reminder rows bumps the parent event's `rev`; the event
   update trigger then logs it.
3. Soft delete (`deleted_utc` NULL → non-NULL) logs `delete`; resurrection logs
   `update`.
4. Hard delete logs `delete` unless `change_context.suppress = 1` (purge path).
5. Remote applies set `origin='remote'` and are pre-marked pushed.
6. Events changed via `rev` bump are logged even if the only visible difference is a
   child row.

```sql
-- Guard: overrides must reference a live master in the same calendar, with same UID.
CREATE TRIGGER trg_events_override_guard
BEFORE INSERT ON events
WHEN NEW.parent_id IS NOT NULL
BEGIN
  SELECT CASE
    WHEN NOT EXISTS (
      SELECT 1 FROM events p
      WHERE p.id = NEW.parent_id
        AND p.parent_id IS NULL
        AND p.calendar_id = NEW.calendar_id
        AND p.uid = NEW.uid
    )
    THEN RAISE(ABORT, 'override parent must be a master in the same calendar with the same uid')
  END;
END;

-- Guard: a master cannot be deleted while overrides still point at it; soft delete only.
CREATE TRIGGER trg_events_no_hard_delete_with_overrides
BEFORE DELETE ON events
WHEN EXISTS (SELECT 1 FROM events c WHERE c.parent_id = OLD.id)
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  SELECT RAISE(ABORT, 'delete overrides before purging a master');
END;

-- Insert: log the new event.
CREATE TRIGGER trg_events_ai
AFTER INSERT ON events
WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     rev, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', NEW.id, NEW.calendar_id, 'insert',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    NEW.rev,
    json_object(
      'id', NEW.id, 'uid', NEW.uid, 'calendar_id', NEW.calendar_id,
      'source_id', NEW.source_id, 'remote_href', NEW.remote_href,
      'etag', NEW.etag, 'rev', NEW.rev, 'sequence', NEW.sequence,
      'summary', NEW.summary, 'rrule', NEW.rrule,
      'start_utc', NEW.start_utc, 'end_utc', NEW.end_utc,
      'start_date', NEW.start_date, 'end_date', NEW.end_date,
      'parent_id', NEW.parent_id, 'recurrence_key', NEW.recurrence_key,
      'status', NEW.status, 'deleted_utc', NEW.deleted_utc),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

-- Update: log only when rev changed (the repo invariant), except soft deletion,
-- which is handled by its own trigger with op='delete'.
CREATE TRIGGER trg_events_au
AFTER UPDATE ON events
WHEN NEW.rev <> OLD.rev
  AND NOT (OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL)
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     rev, before_json, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', NEW.id, NEW.calendar_id, 'update',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    NEW.rev,
    json_object(
      'id', OLD.id, 'uid', OLD.uid, 'calendar_id', OLD.calendar_id,
      'etag', OLD.etag, 'rev', OLD.rev, 'sequence', OLD.sequence,
      'summary', OLD.summary, 'start_utc', OLD.start_utc,
      'end_utc', OLD.end_utc, 'start_date', OLD.start_date,
      'end_date', OLD.end_date, 'status', OLD.status,
      'deleted_utc', OLD.deleted_utc),
    json_object(
      'id', NEW.id, 'uid', NEW.uid, 'calendar_id', NEW.calendar_id,
      'etag', NEW.etag, 'rev', NEW.rev, 'sequence', NEW.sequence,
      'summary', NEW.summary, 'start_utc', NEW.start_utc,
      'end_utc', NEW.end_utc, 'start_date', NEW.start_date,
      'end_date', NEW.end_date, 'status', NEW.status,
      'deleted_utc', NEW.deleted_utc),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

-- Soft delete: op='delete', keeps last state in before_json.
CREATE TRIGGER trg_events_soft_delete
AFTER UPDATE OF deleted_utc ON events
WHEN OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     rev, before_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', NEW.id, NEW.calendar_id, 'delete',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    NEW.rev,
    json_object(
      'id', OLD.id, 'uid', OLD.uid, 'calendar_id', OLD.calendar_id,
      'source_id', OLD.source_id, 'remote_href', OLD.remote_href,
      'etag', OLD.etag, 'rev', OLD.rev, 'sequence', OLD.sequence,
      'start_utc', OLD.start_utc, 'end_utc', OLD.end_utc,
      'start_date', OLD.start_date, 'end_date', OLD.end_date,
      'parent_id', OLD.parent_id, 'recurrence_key', OLD.recurrence_key,
      'status', OLD.status),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

-- Hard delete (purge only; suppress=1 normally).
CREATE TRIGGER trg_events_ad
AFTER DELETE ON events
WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     rev, before_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', OLD.id, OLD.calendar_id, 'delete',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    OLD.rev,
    json_object(
      'id', OLD.id, 'uid', OLD.uid, 'calendar_id', OLD.calendar_id,
      'source_id', OLD.source_id, 'remote_href', OLD.remote_href,
      'etag', OLD.etag, 'rev', OLD.rev, 'sequence', OLD.sequence,
      'start_utc', OLD.start_utc, 'end_utc', OLD.end_utc,
      'start_date', OLD.start_date, 'end_date', OLD.end_date,
      'parent_id', OLD.parent_id, 'recurrence_key', OLD.recurrence_key,
      'status', OLD.status),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;
```

Child-row triggers implement contract rule 2. They must also handle deletion. Note
that a child change on a tombstoned event still bumps `rev`; the event update trigger
skips soft-deletion transitions only, so the log entry is fine.

```sql
CREATE TRIGGER trg_attendees_ai AFTER INSERT ON event_attendees
BEGIN
  UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
  WHERE id = NEW.event_id;
END;

CREATE TRIGGER trg_attendees_au AFTER UPDATE ON event_attendees
BEGIN
  UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
  WHERE id = NEW.event_id;
END;

CREATE TRIGGER trg_attendees_ad AFTER DELETE ON event_attendees
BEGIN
  UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
  WHERE id = OLD.event_id;
END;

CREATE TRIGGER trg_reminders_ai AFTER INSERT ON event_reminders
BEGIN
  UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
  WHERE id = NEW.event_id;
END;

CREATE TRIGGER trg_reminders_au AFTER UPDATE ON event_reminders
BEGIN
  UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
  WHERE id = NEW.event_id;
END;

CREATE TRIGGER trg_reminders_ad AFTER DELETE ON event_reminders
BEGIN
  UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
  WHERE id = OLD.event_id;
END;
```

Calendar and source metadata changes are synced too (calendar creation/rename,
source removal). Same pattern, smaller payloads:

```sql
CREATE TRIGGER trg_calendars_ai
AFTER INSERT ON calendars
WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     after_json, pushed_utc)
  VALUES (
    unixepoch(), 'calendar', NEW.id, NEW.id, 'insert',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    json_object('id', NEW.id, 'source_id', NEW.source_id,
                'remote_id', NEW.remote_id, 'name', NEW.name,
                'default_tz', NEW.default_tz, 'deleted_utc', NEW.deleted_utc),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

CREATE TRIGGER trg_calendars_au
AFTER UPDATE ON calendars
WHEN (NEW.id, NEW.source_id, NEW.remote_id, NEW.name, NEW.description,
      NEW.color, NEW.default_tz, NEW.read_only, NEW.sync_token, NEW.ctag,
      NEW.created_utc, NEW.updated_utc, NEW.deleted_utc)
     IS NOT
     (OLD.id, OLD.source_id, OLD.remote_id, OLD.name, OLD.description,
      OLD.color, OLD.default_tz, OLD.read_only, OLD.sync_token, OLD.ctag,
      OLD.created_utc, OLD.updated_utc, OLD.deleted_utc)
  AND NOT (OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL)
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     before_json, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'calendar', NEW.id, NEW.id, 'update',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    json_object('id', OLD.id, 'name', OLD.name, 'sync_token', OLD.sync_token,
                'ctag', OLD.ctag, 'deleted_utc', OLD.deleted_utc),
    json_object('id', NEW.id, 'name', NEW.name, 'sync_token', NEW.sync_token,
                'ctag', NEW.ctag, 'deleted_utc', NEW.deleted_utc),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

CREATE TRIGGER trg_calendars_soft_delete
AFTER UPDATE OF deleted_utc ON calendars
WHEN OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, calendar_id, op, origin, actor, batch_id,
     before_json, pushed_utc)
  VALUES (
    unixepoch(), 'calendar', NEW.id, NEW.id, 'delete',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    json_object('id', OLD.id, 'source_id', OLD.source_id,
                'remote_id', OLD.remote_id, 'name', OLD.name),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

CREATE TRIGGER trg_sources_ai
AFTER INSERT ON sources
WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, op, origin, actor, batch_id,
     after_json, pushed_utc)
  VALUES (
    unixepoch(), 'source', NEW.id, 'insert',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    json_object('id', NEW.id, 'kind', NEW.kind, 'display_name', NEW.display_name),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

CREATE TRIGGER trg_sources_au
AFTER UPDATE ON sources
WHEN (NEW.id, NEW.kind, NEW.display_name, NEW.config_json,
      NEW.capabilities_json, NEW.created_utc, NEW.updated_utc, NEW.deleted_utc)
     IS NOT
     (OLD.id, OLD.kind, OLD.display_name, OLD.config_json,
      OLD.capabilities_json, OLD.created_utc, OLD.updated_utc, OLD.deleted_utc)
  AND NOT (OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL)
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, op, origin, actor, batch_id,
     before_json, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'source', NEW.id, 'update',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    json_object('id', OLD.id, 'kind', OLD.kind, 'display_name', OLD.display_name),
    json_object('id', NEW.id, 'kind', NEW.kind, 'display_name', NEW.display_name),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;

CREATE TRIGGER trg_sources_soft_delete
AFTER UPDATE OF deleted_utc ON sources
WHEN OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL
  AND (SELECT suppress FROM change_context WHERE id = 1) = 0
BEGIN
  INSERT INTO change_log
    (at_utc, entity_type, entity_id, op, origin, actor, batch_id,
     before_json, pushed_utc)
  VALUES (
    unixepoch(), 'source', NEW.id, 'delete',
    (SELECT origin FROM change_context WHERE id = 1),
    (SELECT actor FROM change_context WHERE id = 1),
    (SELECT batch_id FROM change_context WHERE id = 1),
    json_object('id', OLD.id, 'kind', OLD.kind, 'display_name', OLD.display_name),
    CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
              IN ('remote', 'system')
         THEN unixepoch() END
  );
END;
```

Cache invalidation triggers (fire in addition to change capture):

```sql
CREATE TRIGGER trg_events_invalidate_cache
AFTER UPDATE OF rev ON events
WHEN NEW.rev <> OLD.rev
BEGIN
  DELETE FROM series_state
  WHERE event_id = COALESCE(NEW.parent_id, NEW.id);
  DELETE FROM event_instances
  WHERE event_id = COALESCE(NEW.parent_id, NEW.id);
END;

CREATE TRIGGER trg_events_invalidate_cache_del
AFTER DELETE ON events
BEGIN
  DELETE FROM series_state
  WHERE event_id = COALESCE(OLD.parent_id, OLD.id);
  DELETE FROM event_instances
  WHERE event_id = COALESCE(OLD.parent_id, OLD.id);
END;
```

A subtle point: child-row triggers bump `rev`, which fires cache invalidation once per
child row. Bulk imports therefore bump `rev` several times per event and invalidate
an empty cache repeatedly (cheap). Wrapping imports in one transaction keeps it
correct. If this shows up in profiles, the importer can insert children before the
parent event, or the invalidation trigger can be made `WHEN EXISTS (SELECT 1 FROM
event_instances ...)`; correctness does not depend on the optimization.

### 4.9 Migrations

- One numbered migration per `schema_version` bump, applied inside a single
  `BEGIN IMMEDIATE` transaction with `change_context.suppress = 1`.
- `PRAGMA user_version` mirrors `schema_meta.schema_version` for fast startup checks.
- Migrations that transform event rows must bump `rev` per event, or explicitly
  write `origin='system'` log rows; either way the UI must be told to refresh.
  Preferred: bump `rev`, let triggers log origin `system`, and let the app treat
  `system` changes as refresh-only.
- Never rewrite `etag` during migration unless the remote version changed; doing so
  would cause spurious `If-Match` failures.

### 4.10 Optional full-text search (v1.1)

If search is needed, use an external-content FTS5 table so the source of truth stays
`events`. It is omitted from v1 to keep the trigger surface small. Sketch:

```sql
CREATE VIRTUAL TABLE event_fts USING fts5(
  summary, description, location,
  content = 'events',
  content_rowid = 'rowid',
  tokenize = 'unicode61'
);

CREATE TRIGGER trg_events_fts_ai AFTER INSERT ON events BEGIN
  INSERT INTO event_fts(rowid, summary, description, location)
  VALUES (NEW.rowid, NEW.summary, NEW.description, NEW.location);
END;

CREATE TRIGGER trg_events_fts_ad AFTER DELETE ON events BEGIN
  INSERT INTO event_fts(event_fts, rowid, summary, description, location)
  VALUES ('delete', OLD.rowid, OLD.summary, OLD.description, OLD.location);
END;

CREATE TRIGGER trg_events_fts_au AFTER UPDATE OF summary, description, location ON events BEGIN
  INSERT INTO event_fts(event_fts, rowid, summary, description, location)
  VALUES ('delete', OLD.rowid, OLD.summary, OLD.description, OLD.location);
  INSERT INTO event_fts(rowid, summary, description, location)
  VALUES (NEW.rowid, NEW.summary, NEW.description, NEW.location);
END;
```

---

## 5. Recurrence handling

### 5.1 Semantics we implement

The recurrence set of a master is:

```text
{ occurrences generated by RRULE }
  ∪ { RDATE values }
  − { EXDATE values }
  with any occurrence whose recurrence_key matches an override
    replaced by that override (including CANCELLED = removed)
```

This ordering matters: `COUNT` limits RRULE generation, `EXDATE` removes afterwards
without replenishing, and `RDATE` can add instances beyond `COUNT`. This is RFC 5545
§3.8.5.3 behavior and differs from some naive implementations.

`DTSTART` is always the first occurrence of `RRULE` (unless an `EXDATE` removes it).
`UNTIL` is inclusive; when it is a UTC `DATE-TIME`, compare instants; when it is local,
compare wall times; when it is a `DATE`, compare dates.

### 5.2 Expansion in local time

```text
expand(master, windowStartUtc, windowEndUtc):
    if master.rrule is null:
        return [master] if master overlaps window
    zone      = resolveZone(master.start_tz, master.calendar_tz)
    localFrom = toLocal(max(windowStartUtc, earliestPossible), zone) - safety
    localTo   = toLocal(windowEndUtc, zone) + master.duration
    candidates = rruleIterator(
        rule      = master.rrule,
        dtstart   = parseLocal(master.start_local),
        from      = localFrom,
        until     = min(rule.until, localTo),
        count     = rule.count,
        wkst      = rule.wkst or MO
    )
    out = []
    for localDt in candidates:
        instant = resolveLocal(localDt, zone)     # RFC 5545 §3.3.5 DST policy
        key     = keyFor(instant, master.is_all_day)
        if isExcluded(master.exdate, key, localDt): continue
        out.push(makeInstance(master, key, instant, localDt))
    for rdate in master.rdate:
        instant = resolveRdate(rdate, zone)
        if instant in out: continue
        out.push(makeInstance(master, keyFor(instant), instant, ...))
    overrides = loadOverrides(master.id)
    out = out.map(i => overrides[i.key] ?? i)
              .filter(i => i.status <> 'CANCELLED')
    return sortByStart(out), clippedToWindow(out)
```

`rruleIterator` is a generator over local datetimes, not instants. It must support
the full `FREQ` set, `INTERVAL`, `COUNT`, `UNTIL`, `BYSECOND`, `BYMINUTE`, `BYHOUR`,
`BYDAY` (including ordinal prefixes such as `-1FR`), `BYMONTHDAY` (including
negatives), `BYYEARDAY`, `BYWEEKNO`, `BYMONTH`, `BYSETPOS`, and `WKST`. Use a vetted
library with a conformance suite; do not hand-roll this. Unsupported extensions (e.g.
non-standard `RSCALE` or `SKIP`) are preserved raw, flagged, and expanded with the
standard rule only.

Fast-forwarding: for simple rules (`FREQ=DAILY/WEEKLY/MONTHLY/YEARLY` with no
`BYSETPOS`), jump directly to the first candidate at or after `localFrom` instead of
iterating from `DTSTART`. For complex rules, iterate with a hard candidate cap
(default 100,000) and return a warning if exceeded. This keeps "next occurrence of an
event that started in 1998" cheap.

### 5.3 Duration of occurrences

- Timed: `end = start + duration_seconds` (exact instant delta). A 60-minute meeting
  starting at 01:30 local on a fall-back day lasts 60 real minutes, which may display
  as 02:30 local.
- All-day: `end_date = start_date + (master.end_date − master.start_date)` days.
- An override carries its own start/end and does **not** inherit duration from the
  master.
- `DURATION:P1D` on a timed event is normalized to 86,400 seconds at import, with a
  warning (RFC nominal duration vs exact duration ambiguity; open question Q7).

### 5.4 DST correctness

Cases that must have regression tests:

| Case | Example | Expected |
|---|---|---|
| Spring forward, weekly wall time | `FREQ=WEEKLY;BYDAY=SU;COUNT=4`, US/Eastern, 2026-03-01 09:00 | 03-01 09:00 EST, 03-08 09:00 EDT, 03-15 09:00 EDT, 03-22 09:00 EDT (UTC instants shift by −1h) |
| Fall back, weekly wall time | US/Eastern, 2026-11-01 09:00 | stays 09:00, UTC shifts +1h |
| Nonexistent local time | 2026-03-08 02:30 US/Eastern | interpreted with pre-gap offset → 03:30 EDT |
| Ambiguous local time | 2026-11-01 01:30 US/Eastern | first occurrence (EDT) |
| 30-minute DST | Australia/Lord_Howe | wall time preserved; UTC offset changes by 30 min |
| No DST | Asia/Tokyo | no shift |
| All-day across DST | weekly all-day event | dates unchanged; never becomes 23/25 hours |

### 5.5 Exceptions and overrides

- `EXDATE` matching is by canonical `recurrence_key`, computed from the original
  occurrence start in the master's zone. This avoids string-format mismatches and
  makes `EXDATE;TZID=...` and `EXDATE:...Z` equivalent when they denote the same
  instant.
- An override's `recurrence_id_raw` preserves what the remote sent (for round-trip),
  but matching uses `recurrence_key`.
- An override may change start/end (moved instance), summary, attendees, reminders,
  status. Unspecified fields do not inherit in storage; the importer must copy the
  master's current values into the override when materializing it, because an
  override is a full event resource in iCalendar.
- Deleting one instance locally writes an override with `status='CANCELLED'` and a
  fresh `rev`; it does not touch the master. Deleting the series tombstones the
  master and (soft-)deletes overrides.
- Orphan overrides (parent missing) are a data error: keep the row, mark
  `xprops_json.orphan = true`, hide it from views, and surface a repair action. Never
  drop user data silently.

### 5.6 `RANGE=THISANDFUTURE`

RFC 5545 defines this as "this and all future instances" with a delta applied to
subsequent occurrences. Client support is uneven and semantics for multiple
overlapping ranges are underspecified. v1 policy:

1. Store the override and its range faithfully.
2. During expansion, apply it only to the occurrence matching its `recurrence_key`.
3. Log a warning (`recurrence_range_unsupported`) once per event.
4. Do not rewrite the master's RRULE.

This is deliberately conservative and may be revisited (§10, Q5).

### 5.7 Caching and invalidation

- Default v1: expand on read for the requested window. Typical personal calendars
  (<20k events) expand a month in single-digit milliseconds.
- When `event_instances` is enabled, fill it per series per window; a row is valid
  only if `master_rev = events.rev` and `series_state.tzdb_version` matches the
  current tz database version.
- Triggers invalidate the cache on every `rev` change, so stale rows should be
  impossible; the `master_rev` check is a second line of defense for crashes during
  a rebuild and for restored backups.
- Cache writes never go through `change_context`/change-log triggers because they do
  not modify `events`.

### 5.8 Range-query algorithm

```text
queryRange(fromUtc, toUtc, calendarIds?):
    timed = SELECT * FROM events
            WHERE deleted_utc IS NULL AND parent_id IS NULL
              AND rrule IS NULL
              AND start_utc < :toUtc AND end_utc > :fromUtc
              [AND calendar_id IN (...)]
    masters = SELECT * FROM events
              WHERE deleted_utc IS NULL AND parent_id IS NULL
                AND rrule IS NOT NULL
                [AND calendar_id IN (...)]
    # Optional prefilter, not a correctness requirement:
    #   UNTIL/COUNT bounds from series_state
    instances = timed + flatMap(expand, masters, fromUtc, toUtc)
    allDay = SELECT * FROM events
             WHERE deleted_utc IS NULL AND parent_id IS NULL
               AND is_all_day = 1
               AND start_date < :toDate AND end_date > :fromDate
    return sort(dedupe(instances + allDay))
```

All-day filtering is by date, not instant, so a month view in Tokyo and one in
Honolulu show the same all-day events on the same dates. Timed filtering is by
instant in UTC.

`nextOccurrence(eventId, afterUtc)` reuses the same iterator with `from = afterUtc`
and returns the first instance, used for "next meeting" widgets.

### 5.9 Time zone data versioning

- Record the tz database version in `series_state.tzdb_version` (e.g. `2026a`).
- On startup, if the bundled version differs, delete all `series_state` and
  `event_instances` rows (cheap, derived) and bump a UI refresh token.
- Remote `VTIMEZONE` blobs are stored verbatim in `custom_timezones`; if a remote
  later changes a blob, that is a calendar-level change and invalidates affected
  series.
- Never rewrite stored `start_utc` for existing non-recurring events on tzdb update;
  preserve imported instants and only change future expansion.

---

## 6. Change log and sync substrate

### 6.1 What the log guarantees

1. **Total order**: `seq` is unique and increasing; for a single writer, commit order
   equals `seq` order.
2. **Atomicity**: the data change and its log row(s) commit together or not at all.
3. **Completeness**: triggers capture every `events`/`calendars`/`sources` write,
   including writes made outside the repository.
4. **Attribution**: every row has `origin` and optional `actor`/`batch_id`.
5. **Durability**: log rows survive until explicitly pruned; deletion tombstones keep
   enough state to address the remote object.
6. **Idempotent consumption**: consumers can re-read any range; rows are immutable
   except for push markers.

Non-guarantees (by design): one row per user action (coalescing is allowed), field
level diffs (row snapshots only), or replay to reconstruct history (use backups).

One transaction can emit several rows for the same entity: creating an event with two
attendees and a reminder produces an `insert` plus three `update` rows, one per child
trigger. Consumers coalesce by `(entity_type, entity_id)`; `pending_push` already does
this, and the UI should too. The sequence still reflects the true commit order.

### 6.2 What the app and sync engine do with it

| Consumer | Query | Behavior |
|---|---|---|
| UI / view models | `SELECT ... WHERE seq > cursor` | Refresh affected entities; cursor stored in `sync_cursors` or memory. |
| Push worker | `pending_push` + `change_log` | Push latest state per entity in `seq` order, then mark pushed. |
| Pull worker | remote delta | Apply in one tx with `origin='remote'`, pre-marked pushed. |
| Debug/audit | `change_log` by entity | Show history of an event. |
| Snapshot client | `seq > cursor`; if cursor < `min_available_seq` | Full resync. |

### 6.3 Write path

Application/repository code:

```text
BEGIN IMMEDIATE;
UPDATE change_context
   SET origin = :origin,           -- 'local' | 'remote' | 'system'
       actor = :actor,
       batch_id = :batchId,
       suppress = 0
 WHERE id = 1;
-- ... insert/update/delete events, attendees, reminders ...
COMMIT;
```

Because the context update is a write, a second connection cannot begin its own
`BEGIN IMMEDIATE` until the first commits; therefore one connection's context cannot
leak into another's transaction. Do **not** set the context outside the transaction.
`origin` defaults to `'local'`; forgetting to set it turns a maintenance write into a
harmless extra push rather than silent data loss, but the repository is still required
to set it explicitly on every transaction.

Invariants the repository must uphold:

- Every content change to an event bumps `rev` at least once and sets
  `updated_utc`. (Child tables do this automatically via triggers; direct event
  updates must do it explicitly.)
- Master and overrides are written in the same transaction.
- `events.uid`, `calendar_id`, and `source_id` never change in place; use move
  semantics.
- Soft delete sets `deleted_utc`; it does not clear `etag` (the pusher needs it for
  `If-Match`).
- Bulk operations set one `batch_id` for the whole transaction.

### 6.4 Push path

```sql
-- Latest unpushed change per entity, oldest first.
SELECT l.seq, l.entity_type, l.entity_id, l.op
FROM pending_push p
JOIN change_log l ON l.seq = p.seq
ORDER BY l.seq;
```

For each entity:

1. Load the current aggregate (`event_view` + attendees + reminders) if it still
   exists. If it was hard-deleted, use `before_json` for UID/href/etag.
2. Push to the remote:
   - master and overrides are sent as one resource when the backend supports it
     (CalDAV/iCalendar). Adapters that cannot express overrides may send each
     override as a separate update; this is a sync-design decision.
   - use `If-Match: <etag>` for updates and deletes; use `If-None-Match: *` for
     creates.
3. On success, set `pushed_utc = unixepoch()` for every local unpushed row of that
   entity with `seq <= p.seq`. This coalesces multiple edits into one push.
4. On `412 Precondition Failed`, write a `sync_conflicts` row and stop pushing that
   entity this cycle; continue with other entities.
5. On transient failure, set `push_error`, leave `pushed_utc` NULL, and retry with
   backoff.
6. Update `etag`, `sequence`, and `last_modified_utc` from the response; do **not**
   bump `rev` (that would log a new change). If a subsequent pull sees the same
   etag, it is a no-op.

Deleting a series must push the master tombstone **after** any pending override
changes; sequence order plus coalescing per entity handles this. A hard purge must
not happen while unpushed rows reference the event (§7).

### 6.5 Pull path

```text
fetch remote delta since calendar.sync_token
BEGIN IMMEDIATE;
UPDATE change_context SET origin='remote', actor=:actor, batch_id=:batchId;
for each remote change, in remote order:
    existing = lookup by (calendar_id, remote_href) else (calendar_id, uid, recurrence_key)
    if remote deleted:
        if existing and no unpushed local change: soft-delete
        else: conflict
    else if existing and existing.etag = remote.etag:
        skip (no-op)
    else if existing and hasUnpushedLocal(existing):
        conflict; apply policy
    else:
        upsert event + children; set etag; bump rev
COMMIT;
```

- Upsert keys: `remote_href` first (resource identity), then `(uid, recurrence_key)`.
  This tolerates servers that move resources.
- A remote deletion of an unknown entity is ignored (idempotent replay).
- `hasUnpushedLocal` is:
  `EXISTS (SELECT 1 FROM change_log WHERE entity_type='event' AND entity_id=? AND origin='local' AND pushed_utc IS NULL)`.
- Every applied remote change writes a `change_log` row with `origin='remote'` and
  `pushed_utc` already set, so the UI refreshes and the pusher never echoes it back.
- The whole pull batch is one transaction; cursors (`sync_token`, `sync_batches`)
  advance in the same transaction as the data. If the process crashes mid-pull, the
  batch is replayed and idempotent.

### 6.6 Conflict handling

The core stores the material for a decision; the policy lives in sync design.
Default recommendation:

1. If only one side changed since the last common etag, take that side (no conflict).
2. If both changed, compare `LAST-MODIFIED` (remote) and `updated_utc` (local).
   Newer wins; tie goes to remote.
3. If the loser has meaningful attendee responses, preserve them in
   `sync_conflicts.local_json` and surface a merge prompt; do not silently discard.
4. Resolution writes a normal local change (`origin='local'`, new `rev`, new etag
   after the push) so the loss is visible in the log and the next pull is a no-op.

`events.sequence` is preserved for iTIP semantics; it is not the conflict clock.
`rev` is local-only. Neither replaces `etag` comparison.

### 6.7 Retention and snapshots

- **Log pruning**: delete rows where `pushed_utc IS NOT NULL`
  and `at_utc < now - 30 days`, except keep the highest `seq` per entity if the UI
  might still need it. Update `schema_meta.min_available_seq = MIN(seq)`.
- **Tombstones**: purge events with `deleted_utc < now - 90 days` **and** no unpushed
  change-log rows and no pending delete. Purge in this order: overrides, children,
  master, with `change_context.suppress = 1`.
- **Full resync**: a client whose cursor is below `min_available_seq` must discard
  its mirror and re-read a snapshot at the current `seq`.
- **Never prune unpushed rows.** An event with a permanently failing push stays and
  is visible in an error surface; dropping it would make the remote and local
  silently diverge.
- **Compaction is not allowed to change `seq`.** A pruned gap is fine for range
  readers; it is not fine for cursor readers, hence `min_available_seq`.

### 6.8 Failure modes and how the design covers them

| Failure | Result |
|---|---|
| Crash after local data write | Log row committed with it; nothing to repair. |
| Crash after remote write, before push marker | Push retried; PUT with same UID/href is idempotent; `If-Match` may 412 → conflict path. |
| Crash mid-pull | Single transaction rolls back including cursors; replay is idempotent. |
| Two processes try to write | Second blocks on `BEGIN IMMEDIATE`; no interleaved sequence. |
| Trigger bypassed by `PRAGMA recursive_triggers=OFF`? | Off by default; our triggers do not recurse into the same table. Do not enable recursive triggers globally. |
| Clock moves backwards | `seq` still monotonic; `rev` unaffected; timestamps may look odd but ordering holds. |
| Remote reuses UIDs across calendars | Unique index is per calendar, so allowed. |
| Remote deletes and re-creates same UID | Upsert by href/uid resurrects the tombstone and logs `update`. |

---

## 7. Lifecycle, purge, and retention

```text
create ──► active ──soft delete──► tombstone ──purge──► gone
                ▲                        │
                └────── resurrect ───────┘
```

- **Create**: insert master + children in one tx; `rev = 1`; log `insert`.
- **Update**: mutate; `rev += 1`; log `update`. Overrides: same, but expansion hides
  cancelled ones.
- **Delete one occurrence**: insert/update override with `status='CANCELLED'`.
- **Delete series**: soft-delete master and overrides (`deleted_utc` set, `rev += 1`).
  Keep `etag` and `remote_href` for the delete push.
- **Resurrect**: clear `deleted_utc`, `rev += 1`, new etag after pull; log `update`.
- **Purge** (maintenance, after retention): with `suppress = 1`, delete children,
  then overrides, then master. Never purge an entity with unpushed changes.

Retention defaults (configurable): change log 30 days, tombstones 90 days. Both are
open for review.

---

## 8. Query patterns

All examples assume UTC epoch seconds and a half-open window `[from, to)`.

```sql
-- Timed non-recurring events in a window.
SELECT * FROM events
WHERE deleted_utc IS NULL
  AND parent_id IS NULL
  AND rrule IS NULL
  AND start_utc < :to AND end_utc > :from
ORDER BY start_utc;

-- Recurring masters that may contribute to a window (prefilter).
SELECT * FROM events
WHERE deleted_utc IS NULL
  AND parent_id IS NULL
  AND rrule IS NOT NULL
  AND start_utc < :to
ORDER BY start_utc;

-- All-day events in a date window (end dates exclusive).
SELECT * FROM events
WHERE deleted_utc IS NULL
  AND parent_id IS NULL
  AND is_all_day = 1
  AND start_date < :to_date AND end_date > :from_date
ORDER BY start_date;

-- Overrides for a master.
SELECT * FROM events
WHERE parent_id = :master_id AND deleted_utc IS NULL;

-- Events I am invited to.
SELECT DISTINCT e.*
FROM events e
JOIN event_attendees a ON a.event_id = e.id
WHERE e.deleted_utc IS NULL
  AND a.email = lower(:email)
  AND e.start_utc < :to AND e.end_utc > :from
ORDER BY e.start_utc;

-- Reminder candidates for an expanded window (timed events only; app filters
-- all-day using local midnight and triggers).
SELECT r.*, i.start_utc, i.recurrence_key
FROM event_instances i
JOIN event_reminders r ON r.event_id = COALESCE(i.override_event_id, i.event_id)
WHERE i.start_utc >= :from AND i.start_utc < :to;

-- Unpushed local work.
SELECT * FROM pending_push ORDER BY seq;

-- Changes since a UI cursor.
SELECT * FROM change_log WHERE seq > :cursor ORDER BY seq;
```

Required API surface (language-neutral):

```text
getEvent(id) -> Event | null
queryRange(fromUtc, toUtc, {calendarIds?, includeAllDay?}) -> Instance[]
nextOccurrence(eventId, afterUtc) -> Instance | null
createEvent(input, origin) -> Event
updateEvent(id, patch, expectedRev) -> Event        # 409 on rev mismatch
deleteEvent(id, {occurrenceKey?}) -> void
changesSince(seq) -> Change[]
pendingPush(limit) -> PendingChange[]
markPushed(entityType, entityId, throughSeq)
applyRemote(change, batchId) -> ApplyResult
remindersDue(fromUtc, toUtc) -> ReminderInstance[]
```

---

## 9. Testing strategy

### Schema and invariants

- Every CHECK constraint has a test that must fail: all-day without dates, timed
  without zone, floating with zone, end before start, duration mismatch, override
  without key, override with RRULE, duplicate master UID, duplicate override key,
  suppression flag bypass.
- Composite FK test: inserting an event whose `source_id` disagrees with its
  calendar's source is rejected.
- Override guard: wrong calendar, wrong UID, parent is itself an override → rejected.
- Migration test: v1 fixture upgrades to current, `PRAGMA user_version` and
  `schema_meta` agree, all events have `rev >= 1`.

### Change log

- **No missed writes**: property test that random mutations through the repository
  and through raw SQL both produce exactly one log row (or a coalescible set) per
  event revision.
- **Origin isolation**: remote apply produces log rows with `pushed_utc` set and
  `origin='remote'`; `pending_push` never contains them.
- **Child edits**: attendee add/remove, reminder add/remove bump `rev` and produce an
  event update log.
- **Delete semantics**: soft delete logs `delete` once (not `update` + `delete`);
  resurrection logs `update`; hard delete logs `delete` unless suppressed.
- **Cursor semantics**: reader sees a consistent prefix; concurrent writer does not
  create gaps; `min_available_seq` forces full resync after pruning.
- **Crash simulation**: kill between remote PUT and `markPushed`, verify retry
  idempotence; kill mid-pull, verify rollback.

### Recurrence conformance

- RFC 5545 §3.8.5.3 examples (all of them) as golden tests.
- DST matrix from §5.4.
- `COUNT` + `EXDATE` (does not replenish), `RDATE` beyond `COUNT`, duplicate
  `RDATE`/generated occurrence, `UNTIL` inclusive, `UNTIL` UTC vs local, leap day
  yearly rule, last-Friday `BYDAY=-1FR`, `BYSETPOS`, `WKST`-sensitive weekly rules,
  monthly `BYMONTHDAY=31` skipping short months.
- Overrides: moved instance, cancelled instance, override with changed attendees,
  orphan override, `RANGE=THISANDFUTURE` (documented conservative behavior).
- Round-trip: expand → serialize back to ICS → re-import → identical occurrence set.

### Performance targets

| Scenario | Target |
|---|---|
| 20k events, 5k recurring, month view cold | < 50 ms p95 |
| Same, warm instance cache | < 10 ms p95 |
| Next-occurrence on a 20-year-old infinite series | < 5 ms p95 |
| 100k-row change log, `changesSince` tail | < 10 ms |
| Bulk import 10k events in one tx | < 2 s |

---

## 10. Open questions

Each item states the current default so implementation can proceed; decisions should
be recorded here (or in a linked ADR) when made.

**Q1. Is Hermes the system of record, or a peer?**
Determines whether local edits must always be pushed before a pull is trusted, and
whether `etag` is authoritative. *Default:* peer with etag-based optimistic
concurrency; last-writer-wins on conflict. *Owner:* sync design + Hermes research.

**Q2. Does the Hermes API expose per-resource etags/revisions, or only collection
ctags?**
Per-resource etags give clean conflict detection; collection-only forces full-diff
and local synthetic etags. *Default:* model supports both; adapter may synthesize
`etag` from a hash of the remote payload. *Impact:* conflict precision.

**Q3. Who mints UIDs for local events, and may a UID change?**
*Default:* the core mints a UUIDv4 at create; UID never changes, including on move
between calendars. *Risk:* remote may reject or remap.

**Q4. Which RRULE grammar subset must v1 support?**
Full RFC 5545 is a large surface. *Default:* full grammar via a vetted library, with
`RSCALE`/`SKIP` unsupported and flagged. *Alternative:* P0 subset (FREQ, INTERVAL,
COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH, BYSETPOS, WKST) and reject the rest at
import with a visible warning.

**Q5. Do we support `RANGE=THISANDFUTURE` in v1?**
*Default:* store, apply only to the named occurrence, warn. Full semantics (delta
applied to all future instances, interaction with later overrides) is significant
work.

**Q6. Floating (zone-less) timed events: support, normalize, or reject?**
*Default:* support and store; resolve with the calendar default zone for indexing.
*Risk:* reminders and DST behavior are inherently ambiguous.

**Q7. Duration semantics across DST for timed events.**
*Default:* exact seconds (end − start). *Alternative:* nominal wall-clock duration
(e.g., `P1D` always ends at the same local time). RFC is ambiguous in practice;
clients differ. This affects every recurring timed event that spans a transition.

**Q8. Is the instance cache (`event_instances`) required in v1?**
*Default:* expand on read; enable the cache only if profiling exceeds targets or for
reminder scheduling. Keeping the DDL now fixes invalidation semantics.

**Q9. Where does reminder delivery state live (snooze, dismiss, last-fired)?**
*Default:* not core; the app shell owns a per-device table keyed by
`(event_id, recurrence_key, reminder_id)` and reads `remindersDue`. *Open:* whether
this must sync across devices via Hermes.

**Q10. How long do we retain the change log and tombstones?**
*Default:* 30 days log, 90 days tombstones. Sync design must confirm the maximum
offline window for a client before forcing full resync.

**Q11. Do we store the raw iCalendar blob for round-trip fidelity?**
*Default:* no; model fields plus `xprops_json` for unknown properties. *Trade-off:*
raw blobs make exact round-trip trivial but can go stale and leak unmodeled data.
*Alternative:* store raw only for remote resources, with a checksum.

**Q12. Attendee identity and scheduling scope.**
*Default:* store CAL-ADDRESS plus normalized email, roles, partstat, delegation; do
not send invitations or process replies. *Open:* non-email attendees (rooms,
resources), group expansion, and `SCHEDULE-AGENT`/`SCHEDULE-STATUS` preservation.

**Q13. Attachments (`ATTACH`).**
*Default:* out of scope v1; preserve property in `xprops_json` only. *Open:* fetch
and cache remote attachments (storage, privacy, size limits).

**Q14. Private/confidential event content and encryption at rest.**
*Default:* rely on OS full-disk encryption and SQLite file permissions; do not
encrypt fields. *Open:* whether Hermes requires per-account encryption or redaction
of `PRIVATE` details in notifications/search.

**Q15. Multi-account and calendar sharing semantics.**
*Default:* many sources and calendars; an event belongs to exactly one calendar.
*Open:* shared calendars where the same remote event is visible through multiple
collections (would require an alias table or dedupe by UID + remote href).

**Q16. Time zone source and update cadence.**
*Default:* bundle a tz database, record its version, invalidate derived instances on
change. *Open:* whether the host app or Hermes supplies the tz database, and whether
custom `VTIMEZONE` blobs should ever override IANA when both exist.

**Q17. Do we need field-level change diffs, or are row snapshots enough?**
*Default:* row snapshots plus re-read. *Open:* if the UI wants "what changed" or the
sync engine wants three-way merges, add a `changed_fields` column populated by the
repository (not triggers).

**Q18. `SEQUENCE` policy for local edits.**
*Default:* preserve remote `SEQUENCE`; bump it on local edits to events with
attendees or an organizer, leave it otherwise. *Open:* depends on iTIP scope (Q12)
and remote requirements.

---

## Appendix A — RFC 5545 property mapping

| RFC property | Stored as | Round-trip notes |
|---|---|---|
| `UID` | `events.uid` | Exact string. |
| `DTSTAMP` | `dtstamp_utc` | UTC instant. |
| `DTSTART` | `start_date` (DATE) or `start_local`/`start_tz`/`start_utc` | `TZID` preserved; `Z` → UTC. |
| `DTEND` | `end_date` (DATE) or `end_local`/`end_tz`/`end_utc` | Exclusive. |
| `DURATION` | `duration_seconds` or date delta | Written back as `DTEND` on export; original form not preserved. |
| `SUMMARY` | `summary` | |
| `DESCRIPTION` | `description` | |
| `LOCATION` | `location` | |
| `RRULE` | `rrule` | Value verbatim, uppercase keys recommended. |
| `RDATE` / `EXDATE` | `rdate_json` / `exdate_json` | Canonical RFC value strings; `PERIOD` reduced to start. |
| `RECURRENCE-ID` | `recurrence_id_raw` + `recurrence_key` | `RANGE` in `recurrence_range`. |
| `STATUS` | `status` | |
| `TRANSP` | `transparency` | |
| `CLASS` | `classification` | |
| `ORGANIZER` | `organizer_email`, `organizer_name` | `CN` and `mailto:` preserved partially; other params go to `xprops_json`. |
| `ATTENDEE` | `event_attendees` | Order preserved; `mailto:` in `address`. |
| `CATEGORIES` | `categories_json` | Order preserved. |
| `URL` | `url` | |
| `CREATED` | `created_utc` | |
| `LAST-MODIFIED` | `last_modified_utc` | |
| `SEQUENCE` | `sequence` | |
| `VALARM` | `event_reminders` | Supported subset in §3.7. |
| `ATTACH`, `GEO`, `PRIORITY`, `CONTACT`, `RELATED-TO`, `RESOURCES`, `COMMENT` | `xprops_json` | Preserved raw, not modeled. |
| `X-*` and IANA extensions | `xprops_json` | Preserved raw. |

Export rules:

- Recurring master exports `RRULE`, `RDATE`, `EXDATE`; overrides export
  `RECURRENCE-ID` and share the UID.
- Timed events export `DTSTART;TZID=...` (or `Z` for UTC); floating events export
  bare local `DTSTART`.
- All-day events export `VALUE=DATE` and an exclusive `DTEND`.
- Do not emit a `VTIMEZONE` block when the TZID is in the IANA database unless the
  remote requires it; custom zones come from `custom_timezones`.
- Keep `SEQUENCE`, `DTSTAMP`, and `LAST-MODIFIED` consistent with the push that
  produced them.

## Appendix B — Worked change-log example

Scenario: user creates event `E1` at seq 1; remote changes the summary (seq 2); user
moves it (seq 3); user deletes it (seq 4).

| seq | entity | op | origin | rev | etag | pushed_utc |
|---|---|---|---|---|---|---|
| 1 | E1 | insert | local | 1 | NULL | NULL |
| 2 | E1 | update | remote | 2 | `"r2"` | set at apply |
| 3 | E1 | update | local | 3 | `"r2"` | NULL |
| 4 | E1 | delete | local | 4 | `"r2"` | NULL |

- `pending_push` returns one row for `E1` at seq 4 (latest).
- The pusher loads the tombstone, uses `before_json`'s `etag`/`remote_href`, sends
  `DELETE ... If-Match: "r2"`, then marks seq 3 and 4 pushed (seq 1 and 2 already
  were).
- A UI cursor at 2 receives change 3 and 4 and removes the event from its view.
- After the push, the tombstone remains until the 90-day purge; the change log rows
  remain until the 30-day prune. A client with cursor < `min_available_seq` does a
  full resync instead.

---

*End of draft. Reviewers: please answer or defer Q1–Q18 in §10; the highest-impact
items for implementation are Q1, Q2, Q4, Q7, and Q8.*
