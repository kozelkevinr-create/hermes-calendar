# Hermes Calendar Plugin — Core Model Design

| | |
|---|---|
| Status | Draft v0.2 — revised for storage scopes, multi-writer global store, notifications boundary |
| Branch | `feature/core-design` (cut from `test`) |
| Owner | Core / storage layer |
| Scope | Canonical event model, SQLite schema, recurrence expansion, change log |
| Out of scope | UI, sync transport/protocol details, invitation delivery, free-busy |
| Related | `docs/design/sync.md`, `docs/design/app-notifications.md`, Hermes plugin API research (sibling branches) |

This document is the source of truth for **what an event is** and **how it is stored and
changed**. The app shell, sync engine, notification layer, and external providers all
consume the model and SQL defined here; they should not add parallel event tables.

Hermes is the **host**, not a remote calendar backend. The plugin owns the local
calendar data; external providers are sync peers (see `docs/design/sync.md`).

### Decisions at a glance

- One `events` table is the aggregate root; attendees, reminders, and per-instance
  overrides are children with no independent sync identity.
- Timed events keep **both** the UTC instant and the local wall clock plus IANA zone;
  recurrence expands in local time so DST never moves a 09:00 meeting.
- All-day events are date ranges with an exclusive end date, never instants.
- Recurrence is stored as a master row (`RRULE`/`RDATE`/`EXDATE`) plus override rows
  keyed by the original occurrence start; expansion is lazy, with an optional
  disposable cache.
- **Two physical stores:** one private database per Hermes profile and one shared
  global database. Each has its own `change_log`, `seq`, schema version, and backup
  lifetime; cross-store reads merge in the app or via a read-only `ATTACH`.
- Events live in exactly one store. Internal IDs are UUIDv7, unique across stores;
  references that cross a store use `(store_id, event_id)`.
- A per-store append-only `change_log` with a monotonic `seq`, an `origin`
  (`local`/`remote`/`system`), and soft-delete tombstones is the sync substrate.
- SQLite triggers capture every write into the log; a one-row `change_context` set
  inside each `BEGIN IMMEDIATE` transaction supplies origin/actor/batch.
- SQLite permits **one writer at a time per database file**; WAL lets readers run
  concurrently. The global store is written by several profile processes and relies
  on `BEGIN IMMEDIATE` + `busy_timeout`, not on a single-writer assumption.
- The plugin **is** the Hermes calendar; Hermes is the host. The local calendar is
  the system of record for plugin-created events, and external providers (CalDAV,
  Google, Graph, ICS) are sync peers per `docs/design/sync.md`.
- Reminder **definitions** (`VALARM`) are core; delivery state (instances, lease,
  snooze, attempts) is notification-owned in the per-profile DB (`app-notifications.md`).
- Remote version tokens (`etag`) are opaque and preserved, never parsed; local-only
  events have no etag.

---

## Contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Design principles](#2-design-principles)
3. [Canonical event model](#3-canonical-event-model)
4. [Storage scopes](#4-storage-scopes)
5. [SQLite schema](#5-sqlite-schema)
6. [Recurrence handling](#6-recurrence-handling)
7. [Change log and sync substrate](#7-change-log-and-sync-substrate)
8. [Lifecycle, purge, and retention](#8-lifecycle-purge-and-retention)
9. [Query patterns](#9-query-patterns)
10. [Testing strategy](#10-testing-strategy)
11. [Open questions and decision log](#11-open-questions-and-decision-log)

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
5. Run entirely on local SQLite files, with no background server required; handle
   several profile processes writing the shared global store safely.
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

- **Hermes is the host.** The plugin runs inside or beside Hermes, uses
  `plugin_data_dir()`/`plugin_db()` for its profile store, and may use the shared
  root helper for the global store (§4). Hermes exposes no calendar API and is never
  treated as a sync peer.
- **Two storage scopes** (§4): one profile database per profile and one shared global
  database. In normal operation one plugin process serves a profile, but several
  profile processes may write the global store concurrently.
- **SQLite serializes writers per file.** There is no application-level single-writer
  guarantee for the global store; `BEGIN IMMEDIATE`, WAL, and `busy_timeout` make
  concurrent writers safe. A writer that exceeds the timeout must retry (§4.8).
- **External sync peers** expose a stable event identifier, a version token
  (etag/revision), and calendar-level change enumeration. If a peer can only re-list a
  whole collection, the sync engine synthesizes etags and the core still works.
- **The global store path is proposed, not verified.** `<get_default_hermes_root()>/
  plugin-data/<name>/` is unverified under `plugins.isolation: host` (Q19). The
  profile store uses the verified `plugin_data_dir()` behavior.
- The notification layer owns delivery state; the core owns reminder definitions and
  expands occurrences (§3.7, §5.11).

---

## 2. Design principles

1. **UTC for instants, wall clock for recurrence.** Every absolute point in time is
   stored as an epoch-seconds integer. Recurring events additionally store their
   original local wall time and IANA time zone, because RFC 5545 expands in local
   time and DST must not shift a 09:00 meeting to 08:00.
2. **The event is the aggregate root.** Attendees, reminders, exceptions, and
   overrides have no independent sync identity; a change to any of them bumps the
   event's `rev`. One event aggregate (master plus overrides) = one logical record.
3. **Preserve what we do not understand.** Unknown/extension iCalendar properties are
   kept in `events.xprops_json` for round-trip. Unknown RRULE parts are preserved
   verbatim and flagged rather than dropped.
4. **Derived data is disposable.** Expanded instances, series checkpoints, and
   search indexes can be deleted and rebuilt without data loss.
5. **Every write has an origin and a scope.** A write is `local`, `remote`, or
   `system`, and belongs to either the profile store or the global store. Origin
   prevents remote pulls from being pushed straight back; scope prevents private
   data from leaking into shared data.
6. **The change log is append-only and per store.** It is written in the same
   transaction as the data change, ordered by a monotonic sequence local to that
   store, and never updated except to mark rows as pushed.
7. **Version tokens are opaque.** `etag` and remote cursors are never parsed for
   meaning, only compared.
8. **The database enforces invariants where cheap.** Checks, partial unique indexes,
   and triggers catch bugs that application code will eventually introduce.
9. **No cross-store transactions.** An event lives in exactly one store; operations
   that must span stores use an idempotent intent record and reconciliation, never
   an assumed atomic commit.

---

## 3. Canonical event model

### 3.1 Field summary

`Req` is required in the model. Storage names are the SQLite columns from §5.

| Field | Type | Req | RFC 5545 source | Storage | Notes |
|---|---|---|---|---|---|
| `id` | opaque ID (UUIDv7) | yes | — | `events.id` | Internal identity. Stable for the life of the row, never sent remotely; globally unique across stores. |
| `scope` | enum | yes | — | `events.scope` | `profile` or `global`; must match the store (§4.6). |
| `owner_profile_id` | string \| null | cond. | — | `events.owner_profile_id` | Profile that created the row. Required in profile stores; set to the creator in the global store. |
| `uid` | string | yes | `UID` | `events.uid` | Unique per calendar, not per store. Preserved exactly, including case. Master and its overrides share it. |
| `summary` | string | yes | `SUMMARY` | `events.summary` | Empty string when absent; not NULL, to keep UI simple. |
| `description` | string \| null | no | `DESCRIPTION` | `events.description` | |
| `location` | string \| null | no | `LOCATION` | `events.location` | |
| `start` | instant + tz, or date | yes | `DTSTART` | `start_utc`, `start_local`, `start_tz`, `start_date` | See §3.4. |
| `end` | instant + tz, or date | yes | `DTEND` / `DURATION` | `end_utc`, `end_local`, `end_tz`, `end_date`, `duration_seconds` | `DTEND` is exclusive. `DURATION` is normalized to seconds. |
| `tz` | IANA name \| null | cond. | `TZID` parameter | `start_tz`, `end_tz` | NULL means floating; all-day events are date-based. |
| `all_day` | bool | yes | `VALUE=DATE` | `events.is_all_day` | Date-only event. End date exclusive (§3.5). |
| `floating` | bool | yes | no `TZID`, no `Z` | `events.is_floating` | Timed event pinned to local wall time everywhere. |
| `rrule` | string \| null | no | `RRULE` | `events.rrule` | Verbatim value without the `RRULE:` prefix. |
| `rrule_unsupported` | array \| null | no | — | `rrule_unsupported_json` | Parts outside the P0 subset (§6.2); preserved and surfaced, not silently ignored. |
| `rdate` / `exdate` | array \| null | no | `RDATE` / `EXDATE` | `rdate_json`, `exdate_json` | JSON arrays of RFC value strings. |
| `recurrence_id` | string \| null | cond. | `RECURRENCE-ID` | `recurrence_id_raw`, `recurrence_key` | Present only on overrides. Key is the **original** occurrence start. |
| `recurrence_range` | `THIS` \| `THISANDFUTURE` \| null | no | `RANGE` parameter | `recurrence_range` | `THISANDFUTURE` support is limited in v1 (§6.6). |
| `reminders` | array | no | `VALARM` | `event_reminders` | Relative or absolute triggers; display/audio/email actions. |
| `attendees` | array | no | `ATTENDEE` | `event_attendees` | Ordered, with role, partstat, RSVP, delegation. |
| `organizer` | object \| null | no | `ORGANIZER` | `organizer_email`, `organizer_name` | |
| `source` | object | yes | — | `events.source_id` → `sources`; `calendar_id`; `remote_href` | `source.kind` is `local`, `caldav`, `google`, `graph`, or `ics`; local is the plugin's own data. FK-protected (§3.8). |
| `etag` | string \| null | cond. | `getetag` (CalDAV) / revision | `events.etag` | Opaque external-peer version. NULL for local-only events. |
| `rev` | integer | yes | — | `events.rev` | Local revision; bumped on every aggregate mutation. Drives cache invalidation and conflict detection. |
| `sequence` | integer | yes | `SEQUENCE` | `events.sequence` | iCalendar semantic sequence. Default 0. |
| `status` | enum | yes | `STATUS` | `events.status` | `TENTATIVE` / `CONFIRMED` / `CANCELLED`. Default `CONFIRMED`. |
| `transparency` | enum | yes | `TRANSP` | `events.transparency` | `OPAQUE` / `TRANSPARENT`. Default `OPAQUE`. |
| `classification` | enum | yes | `CLASS` | `events.classification` | `PUBLIC` / `PRIVATE` / `CONFIDENTIAL`. |
| `url` | string \| null | no | `URL` | `events.url` | |
| `categories` | array | no | `CATEGORIES` | `categories_json` | JSON array of strings. |
| `created` / `updated` | instant \| null | no | `CREATED`, `LAST-MODIFIED` | `created_utc`, `last_modified_utc` | `updated_utc` is local bookkeeping; `last_modified_utc` is the remote value. |
| `dtstamp` | instant \| null | no | `DTSTAMP` | `dtstamp_utc` | |
| `deleted` | instant \| null | no | — | `events.deleted_utc` | Soft-delete tombstone; row physically purged later (§8). |
| `xprops` | object | no | any unmodeled property | `xprops_json` | Preserved for round-trip. |

### 3.2 Canonical JSON shape

This is the shape handed between core, app shell, and adapters. Adapters may not
invent fields; they map into this shape.

```json
{
  "id": "01933cf4-7a2e-7c3b-9d1f-2a4b6c8d0e1f",
  "scope": "profile",
  "ownerProfileId": "coding",
  "uid": "0f3c2b1a-9d8e-4c7b-a6f5-2e1d0c9b8a76",
  "calendarId": "cal_work",
  "source": {
    "id": "src_local",
    "kind": "local",
    "calendarId": "cal_work",
    "remoteHref": null
  },
  "etag": null,
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
- `scope` is store-defined (`profile` or `global`); `ownerProfileId` is the profile
  that created the row. Cross-store references use `(storeId, id)`, where `storeId`
  is `profile:<name>` or `global`.

### 3.3 Identity and uniqueness

| Identifier | Scope | Mutability | Purpose |
|---|---|---|---|
| `events.id` | both stores | never | Local joins, overrides, cache keys; UUIDv7, unique across stores. |
| `(store_id, events.id)` | system | never | Identity for anything that outlives one store. |
| `events.uid` | calendar | stable; changes only if a peer changes it | RFC identity and external-peer matching. Not unique across stores. |
| `(calendar_id, uid)` for master | calendar | unique | One master per UID per calendar. |
| `(parent_id, recurrence_key)` | series | unique | One override per occurrence. |
| `remote_href` | calendar | stable in practice | External resource locator. Master and overrides share it; NULL for local-only events. |
| `etag` | event | every external change | Optimistic concurrency with a sync peer; NULL locally. |
| `rev` | event | every local/remote change | Cache invalidation, dirty detection, and cross-profile compare-and-swap. |

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
  perspective. The core keeps the same `events.id` and `uid` only if the peer
  supports move; otherwise it tombstones and re-creates. The sync design decides
  which path a given adapter uses.
- Moving an event between **stores** is always a copy with a new internal `id`; see
  §4.6 for UID rules.
- Duplicate `UID`s inside one remote collection are a protocol violation. Importers
  keep the first master and log a warning; overrides are still accepted.
- The same `UID` in two different calendars or stores is legal and must not be used
  to deduplicate across stores.

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
   used; a version change invalidates derived instances (§6.9).

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

### 3.7 Reminders: definitions vs delivery state

The core stores **reminder definitions** (`VALARM`), not delivery state. This is a
deliberate split with `docs/design/app-notifications.md`:

| Concern | Owner | Tables |
|---|---|---|
| What the event asks for (`VALARM`) | core | `event_reminders` |
| When an occurrence actually fires | notifications | `reminder_instances` |
| Attempts, success/failure, provider ids | notifications | `notification_deliveries` |
| Channels, Web Push subscriptions, preferences | notifications | `notification_channels`, `web_push_subscriptions`, `notification_prefs` |
| Lease, snooze, dismiss, retry, state machine | notifications | columns on `reminder_instances` |

The core exposes definitions and expanded occurrences; the notification layer
materializes one `reminder_instances` row per `(store, event, reminder definition,
occurrence)` and owns the state machine, lease, and delivery log. Both layers live in
the **same per-profile SQLite database**; the global store has no notification state.

`VALARM` support in the core v1 subset:

| RFC concept | Support |
|---|---|
| `ACTION:DISPLAY`, `AUDIO`, `EMAIL` | Stored. Delivery of EMAIL/AUDIO is the notification layer's responsibility. |
| `TRIGGER` relative (`-PT10M`) | Full. Stored as negative offset seconds. |
| `TRIGGER` absolute (`VALUE=DATE-TIME`) | Full. Stored as UTC instant. |
| `RELATED=START` / `RELATED=END` | Full. |
| `REPEAT` + `DURATION` | Stored as `repeat_count` + `repeat_interval_seconds`. |
| `ACKNOWLEDGED` | Not core; delivery state. |

Multiple identical reminders on one event are deduplicated by the unique constraint.
Resolved Q9: reminder delivery state is **not** core data. The reconciled tables and
field-level naming conflicts are specified in §5.11; `app-notifications.md` remains
the owner of the delivery semantics.

### 3.8 Source, etag, and the Hermes role

Hermes is the host process, not a calendar service. There is no "Hermes calendar API"
to sync against. `source` describes where an event's data lives:

```text
source = { id, kind: 'local' | 'caldav' | 'google' | 'graph' | 'ics',
           calendarId, remoteHref }
```

- `kind = 'local'` means plugin-owned data: the profile calendar or the global
  calendar. Local events have `etag = NULL`, `remote_href = NULL`, and are the system
  of record for plugin-created events.
- The other kinds are **external sync peers** implementing `docs/design/sync.md`
  (CalDAV, Google Calendar API, Microsoft Graph, ICS feed). For those, `etag` is the
  peer's version token and `remote_href` is the resource locator.
- A linked calendar still keeps its canonical row locally; the peer is authoritative
  only for the conflict rules in `sync.md`. The core does not call providers; the
  sync engine writes through the same repository API.
- `events.source_id` is denormalized from `calendars.source_id` for cheap filtering,
  but a composite foreign key `(calendar_id, source_id)` → `calendars(id,
  source_id)` makes inconsistency impossible. A second composite key
  `(calendar_id, scope)` binds the event to its store scope (§4.6).
- `source.kind` is authoritative for adapter behavior. Adding a kind is a schema
  migration only because of the `CHECK` constraint.
- `etag` is compared, never parsed, and preserved even when the event body is
  otherwise unchanged so a subsequent push can use `If-Match`.
- `rev` is local and monotonic. Every mutation batch must increment it **at least
  once** (child-table triggers add one bump per child row, so an event plus three
  attendees ends at `rev = 4`). The change-log triggers are keyed on `rev` changes;
  a mutation without any `rev` bump is a bug and is caught by tests (§10). `rev` is
  an opaque revision counter, not a change count: only equality matters to readers,
  and it doubles as the compare-and-swap token for multi-profile writes.
- `calendars.sync_token` / `calendars.ctag` are a peer collection cursor and
  collection version; their interpretation belongs to the sync engine.

Ownership boundary with `docs/design/sync.md`: core `sources`/`calendars` are the
storage for sync's account/collection concepts, and `change_log` is the durable
outbox substrate (`pending_push`). Sync-specific projections (outbox operations,
conflict records, revisions) may be views over the change log or sync-owned tables;
they must not create a second `events` table. `sync_conflicts` here is the storage
slot for sync's conflict state, not a competing conflict policy.

---

## 4. Storage scopes

Hermes is a multi-profile host, and the calendar has two visibility scopes:

- **Profile calendar** — private to one Hermes profile. Each profile owns exactly one.
- **Global calendar** — one shared calendar that every profile can read and write.

These are different trust and lifetime domains: deleting a profile must not delete
shared events, and one profile crashing or migrating must not take the others'
calendars with it. Storage topology is therefore part of the core design, not an
adapter detail.

### 4.1 Verified host facts and the proposed global path

Verified on the Hermes host for this project:

- `plugin_data_dir(name)` resolves to `<hermes home>/plugin-data/<name>/` and follows
  the **active profile**; profiles live at `~/.hermes/profiles/<profile>/` (current
  profiles: `coding`, `local`, `pentest`).
- `plugin_db()` opens an SQLite database in that plugin data directory in WAL mode.
- Hermes exposes a shared-root helper, `get_default_hermes_root()`.
- **Proposed, unverified:** the global store lives at
  `<get_default_hermes_root()>/plugin-data/<name>/calendar-global.db`. Whether this
  path is reachable when `plugins.isolation: host` is not guaranteed; under stricter
  isolation modes each plugin instance may see only its own data directory. This is
  a blocking deployment question (Q19), not a reason to block the schema.

Consequences that must be handled by the plugin, not assumed away:

1. `plugin_data_dir(name)` is **profile-relative**. A long-running process started
   under `coding` cannot use the helper to reach `pentest`'s database; it must resolve
   the target profile explicitly (environment or profile-aware helper). Background
   jobs (including the reminder cron) must be started with the intended profile.
2. Profile identity is the directory name in v1 (`coding`, `local`, `pentest`). A
   rename would change the identity; see Q20.
3. The global store has a different lifetime from every profile store. It needs its
   own schema version, backup policy, and ownership rules.

### 4.2 Option A — two physical stores (recommended)

```text
$HERMES_HOME/plugin-data/hermes-calendar/calendar.db            profile store (one per profile)
$HERMES_ROOT/plugin-data/hermes-calendar/calendar-global.db     global store (one, shared)
```

Both stores use the **same schema** (this document), with a different value in
`schema_meta.store_scope`. The global store contains exactly one global calendar
(`scope = 'global'`, `owner_profile_id` NULL) and the sources/events that belong to
it. The profile store contains the profile's calendar (`scope = 'profile'`) and its
own local or externally synced calendars. An event lives in **exactly one** store.

Advantages:

- **Isolation is physical.** A corrupted profile database, a profile reset, or a
  profile backup cannot damage the global calendar, and vice versa.
- **Writes are independent.** Profiles writing their own calendars never contend
  with each other; only global-calendar writes contend.
- **Lifetime matches ownership.** Deleting a profile is a file delete for its own
  data; global rows are explicitly orphaned rather than cascaded.
- **No schema merge conflict.** A profile can run a newer plugin build while another
  profile is on an older one; each store migrates independently. The global store
  must remain backward-compatible across the versions that can run concurrently
  (see §5.9).
- `plugin_db()` and `plugin_data_dir()` work unchanged for the profile store.

Costs:

- Cross-store reads need two queries (or `ATTACH`) and application-level merging.
- No atomic transaction spans both stores under WAL (§4.4).
- The global path depends on `get_default_hermes_root()` and isolation behavior
  (Q19).

### 4.3 Option B — one database with a `scope` column

All profiles open the same database file; every row carries `scope = 'profile' |
'global'` and, for profile rows, `owner_profile_id`.

Advantages:

- One change log, one `seq`, one cursor, one schema version.
- Cross-scope queries and transactions are trivial (`UNION ALL`, one `BEGIN`).
- Simpler backup and migration story: one file.

Costs:

- **Every profile serializes on the same SQLite writer**, including ordinary
  profile-only edits. SQLite still permits only one write transaction at a time, so
  a slow import in one profile blocks calendar edits in every other profile.
- **Isolation is application-enforced.** Any profile process can read every other
  profile's events with one query; a bug in a `WHERE scope = ...` clause leaks
  private data. There is no OS or file boundary.
- Deleting a profile becomes a mass delete with cascade/orphan questions, not a
  file operation, and the `plugin_data_dir()`-per-profile contract no longer maps
  to storage.
- One corruption or failed migration takes down every profile.
- The database must be reachable from every profile, so the isolation problem in
  Q19 applies to the *entire* calendar, not only the global one.

### 4.4 Decision

**Use two physical stores (Option A).** The isolation and failure-domain benefits
outweigh the query-merge cost, and the option works with the verified
`plugin_data_dir()` behavior without depending on an unverified shared-path
guarantee for private data.

Rules that follow from the decision:

1. **One event, one store.** Publishing a profile event to the global calendar
   creates a *copy* in the global store; it is not a pointer or a mirrored row. The
   copy gets a new internal `id`; see §4.6.
2. **No cross-store foreign keys.** `events.calendar_id` never references a calendar
   in the other store. Inter-store references carry `(store_id, entity_id)`.
3. **Cross-store writes are not atomic.** WAL provides atomicity per database file,
   not across `ATTACH`ed files. A publish/delete-copy operation writes an intent row
   in the initiating store, performs the second-store write, then marks the intent
   complete; replay is idempotent. The window is visible in the change log and is
   recoverable, but it is not a distributed transaction.
4. **The global store is shared mutable state.** Any profile may insert or update
   global events. Mutations use compare-and-swap on `rev` (§5.3 and §7.3); a
   read-modify-write without `expectedRev` is a bug.
5. **Global external sync is single-owner.** If the global calendar is linked to an
   external provider, exactly one process may push it. Elect a sync owner with a
   lease row in the global store (same mechanism as the reminder lease in
   `app-notifications.md`); other profiles only read/write rows, never sync. See
   Q22.

### 4.5 Querying across stores

Two supported patterns:

**Application-level merge (recommended, and the only pattern for writes).**
Open one connection per store (the global connection read-only unless the operation
explicitly writes global data), run the same range query in each, and merge the
result sets in memory. Both stores use identical time semantics, so sorting by
`(start_utc, start_date, id)` is well-defined.

```text
results = query(profileDb, from, to)
        + query(globalDb, from, to)
sort by start instant (all-day dates sort at local midnight)
dedupe by (store_id, event_id)      # never by UID across stores
```

**`ATTACH DATABASE` (optional, read-only).**
A single connection may attach the other store and use `UNION ALL` views. This is
convenient for free/busy or search, but:

- `ATTACH` is per connection, and it must happen outside a transaction.
- Under WAL, a transaction that touches two attached databases is **not atomic**;
  SQLite commits each file separately.
- The global file may be absent or permission-denied; a failed attach must degrade
  to profile-only results, not fail the whole view.
- Never use an attached write transaction for publish/move; use the intent log in
  §4.4.

The global connection opens with `mode=ro` for read-merge paths
(`file:...?mode=ro&immutable=0`) so a UI bug cannot write shared state.

### 4.6 How the schema marks scope and keeps IDs unique

- `schema_meta.store_scope` (`profile` | `global`), `schema_meta.store_id`
  (`profile:coding` | `global`), and `schema_meta.owner_profile_id` identify the
  store. `store_id` is the prefix used in cross-store references.
- `sources`, `calendars`, `events`, and `change_log` carry an explicit `scope`
  column, defaulted to `profile` and constrained to the two values. In the
  recommended two-store design it is constant per database; it exists so exports,
  backups, and a future one-database migration cannot lose provenance.
- `calendars` and `events` also carry `owner_profile_id`. In the profile store it is
  the active profile; in the global store it is the profile that created the row
  (edits by other profiles are recorded in `change_log.actor`).
- Composite foreign keys plus guard triggers make the scope columns agree:
  `calendars (id, scope)` and `sources (id, scope)` are unique; `events
  (calendar_id, scope)` references `calendars (id, scope)`; a trigger rejects a
  calendar/source whose scope differs from `schema_meta.store_scope` (§5.8).
- **Internal IDs are UUIDv7**, minted locally, time-sortable, and unique across both
  stores without coordination. They are not prefixed with the scope: the 128-bit
  space is large enough, and a prefix would complicate external references.
- **System-wide identity is `(store_id, entity_id)`.** Anything that can outlive one
  store — notification delivery rows, deep links, publish-intent rows, diagnostics —
  stores the pair. Inside a store, `events.id` remains the primary key.
- **`uid` is calendar-scoped, not store-scoped.** Copying an event between stores
  mints a new internal `id`. Keep the source `uid` only when the destination is a
  local-only calendar with no UID conflict; otherwise mint
  `<uuidv7>@hermes.local`. This prevents a published copy from being mistaken for
  the same remote resource. Never merge two events because their UIDs match across
  stores.

### 4.7 Change log and `seq` per store

Each store owns an independent `change_log` with its own `seq` sequence:

- `seq` starts at 1 in every new store and is scoped to that store. A cross-store
  cursor is `(store_id, seq)`, never a bare integer.
- The UI keeps one cursor per store and merges the resulting changes by event start
  time for display. There is no global ordering across stores; comparing `seq` values
  from two databases is meaningless.
- Each store has its own `min_available_seq` and snapshot boundary (§7.7). A profile
  store can be compacted independently of the global store.
- The global store is multi-writer. SQLite serializes its write transactions, so
  `seq` order still equals commit order: a writer holds the write lock from before
  its first `change_log` insert until commit, and no other writer can insert or
  commit in between. Readers in WAL see a consistent committed prefix, and rolled
  back transactions merely leave gaps in `seq` (AUTOINCREMENT never reuses values).
- The profile store is single-process in normal operation but may still see the
  plugin service and the reminder cron writer; the same serialization rule applies.
  Do not build logic that assumes one writer process, only one writer **at a time**.

### 4.8 Failure modes and operating rules for the global store

| Condition | Effect | Required behavior |
|---|---|---|
| Global store missing/unmounted | Global calendar invisible | Run profile-only, show a persistent "global calendar unavailable" banner; do not silently write global events into the profile store |
| `plugins.isolation` hides the shared root | Global store cannot open | Degrade as above; this is the Q19 blocker |
| Another profile holds the write lock | Writers wait up to `busy_timeout` | Retry with jitter; surface a warning after repeated timeouts; never disable WAL to "fix" it |
| Long-running write (bulk import, huge sync) | Other profiles block; WAL grows; checkpoints starve | Chunk work into short transactions; keep `busy_timeout` ~10 s; checkpoint between chunks |
| Global DB on NFS/SMB | Hermes falls back to DELETE journal mode; POSIX locks may be unreliable | Refuse multi-profile writes on network storage, or designate one writer process and treat everyone else read-only; WAL must not be forced |
| Two profiles update the same event | One may commit a stale read-modify-write | Compare-and-swap on `rev`; return 409 and let the caller re-read |
| Global DB schema version newer than this build | Queries may fail | Refuse to open/migrate; require a plugin upgrade rather than silently downgrading |
| Profile DB deleted | Profile calendar gone; global rows remain | Keep global events; show them with `owner_profile_id` pointing at the missing profile (Q20) |

`busy_timeout` is a wait, not a queue: a writer that exceeds it gets
`SQLITE_BUSY` and must retry with backoff. Readers in WAL are not blocked by a
writer, but in DELETE mode (network filesystem) they are, which is why the global
store must be local.

---

## 5. SQLite schema

Target: SQLite **3.38+** (uses `unixepoch()`, JSON1, partial indexes, strict-ish
checks). Every DDL block in this document has been executed against SQLite 3.45 as a
whole; CI should keep doing exactly that so the document cannot rot. Schema version is
tracked both in `schema_meta` and `PRAGMA user_version`.

### 5.1 Connection pragmas

Applied on every connection open:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 10000;
PRAGMA temp_store = MEMORY;
PRAGMA user_version = 1;
```

`journal_mode` is a request, not a guarantee: if the file is on a filesystem where
WAL is not safe (the shared-root/network case in §4.8), Hermes observes a fallback to
`DELETE` mode. The plugin must check and report the actual mode, never force WAL on a
network mount. `busy_timeout` is 10 s for the global store (several profile writers);
the per-profile store may use the same value without harm. `synchronous = NORMAL` is
the usual WAL trade-off: a power loss can lose the last transactions but cannot
corrupt the database; use `FULL` if the host cannot tolerate losing the tail.

Write transactions use `BEGIN IMMEDIATE` so the write lock is acquired before any
reads, avoiding `SQLITE_BUSY` upgrade failures and making sequence assignment
serialized (§7.3). This is a per-database lock: it is the mechanism that makes the
shared global store safe with several profile processes, not an application-level
single-writer assumption.

### 5.2 Metadata and sources

```sql
CREATE TABLE schema_meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO schema_meta (key, value) VALUES
  ('schema_version', '1'),
  ('created_by', 'hermes-calendar'),
  ('min_available_seq', '1'),
  ('store_scope', 'profile'),          -- 'global' in the shared store
  ('store_id', 'profile:unset'),       -- 'profile:coding' or 'global'
  ('owner_profile_id', 'unset');       -- global store sets this to ''

CREATE TABLE sources (
  id                TEXT PRIMARY KEY,
  scope             TEXT NOT NULL DEFAULT 'profile'
                    CHECK (scope IN ('profile', 'global')),
  owner_profile_id  TEXT,
  kind              TEXT NOT NULL
                    CHECK (kind IN ('local', 'caldav', 'google', 'graph', 'ics')),
  display_name      TEXT NOT NULL,
  config_json       TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(config_json)),
  capabilities_json TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(capabilities_json)),
  created_utc       INTEGER NOT NULL,
  updated_utc       INTEGER NOT NULL,
  deleted_utc       INTEGER,
  UNIQUE (id, scope),
  CHECK (scope = 'global' OR owner_profile_id IS NOT NULL),
  CHECK (deleted_utc IS NULL OR deleted_utc >= created_utc)
);

CREATE TABLE calendars (
  id                TEXT PRIMARY KEY,
  source_id         TEXT NOT NULL,
  scope             TEXT NOT NULL DEFAULT 'profile'
                    CHECK (scope IN ('profile', 'global')),
  owner_profile_id  TEXT,
  remote_id         TEXT,
  name              TEXT NOT NULL,
  description       TEXT,
  color             TEXT,
  default_tz        TEXT NOT NULL DEFAULT 'UTC',
  read_only         INTEGER NOT NULL DEFAULT 0 CHECK (read_only IN (0, 1)),
  sync_token        TEXT,
  ctag              TEXT,
  created_utc       INTEGER NOT NULL,
  updated_utc       INTEGER NOT NULL,
  deleted_utc       INTEGER,
  UNIQUE (id, source_id),
  UNIQUE (id, scope),
  UNIQUE (source_id, remote_id),
  CHECK (scope = 'global' OR owner_profile_id IS NOT NULL),
  FOREIGN KEY (source_id, scope)
    REFERENCES sources (id, scope)
    ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE custom_timezones (
  tzid          TEXT PRIMARY KEY,
  source_id     TEXT REFERENCES sources(id) ON DELETE CASCADE,
  vtimezone_ics TEXT NOT NULL,
  updated_utc   INTEGER NOT NULL
) WITHOUT ROWID;
```

`UNIQUE (id, source_id)` and `UNIQUE (id, scope)` exist solely to be the parent keys
of the composite foreign keys on `calendars`/`events`. SQLite requires a unique index
whose columns match each FK target exactly.

The store initializer sets `store_scope`, `store_id`, and (for a profile store)
`owner_profile_id` before inserting any calendar; the global store sets the owner key
to an empty string. The global store seeds exactly one calendar with
`scope = 'global'`. A profile store is seeded with exactly one local `Personal`
calendar (schema v3) when it has never held any calendar, including deleted rows, so a
client that can add events but cannot create calendars has somewhere to write; the seed
is logged as a `system` change and is never pushed to a remote provider. A profile that
already has a calendar, or has deliberately deleted its only calendar, gets nothing
back. `sources.kind = 'local'` is the plugin's own data; the other kinds
are external sync peers. There is no `'hermes'` kind because Hermes does not expose a
remote calendar API.

### 5.3 Events

```sql
CREATE TABLE events (
  id                TEXT PRIMARY KEY,
  calendar_id       TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  scope             TEXT NOT NULL DEFAULT 'profile'
                    CHECK (scope IN ('profile', 'global')),
  owner_profile_id  TEXT,
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
  rrule_unsupported_json TEXT CHECK (rrule_unsupported_json IS NULL
                                     OR json_valid(rrule_unsupported_json)),
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
  FOREIGN KEY (calendar_id, scope)
    REFERENCES calendars (id, scope)
    ON UPDATE CASCADE ON DELETE CASCADE,
  CHECK (scope = 'global' OR owner_profile_id IS NOT NULL),

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

- `scope`/`owner_profile_id` are redundant with the database boundary on purpose:
  they make exports, backups, and a one-database migration self-describing, and the
  composite FK prevents an event from disagreeing with its calendar. In the
  recommended design every row in a store has the same `scope`.
- `end_local`/`end_tz`/`end_utc` are populated for every timed event when the source
  supplied `DTEND` (or it can be derived). `end_utc` and `duration_seconds` are
  always populated and drive queries and recurrence math; `end_local`/`end_tz`
  preserve the original wall-clock end for round-trip.
- `end_utc - start_utc = duration_seconds` is intentionally strict. It defines
  duration as an exact instant delta, which is what recurrence expansion needs.
  Nominal-day durations across DST are converted once at import (§6.4).
- `rev` is the change token. It doubles as the compare-and-swap token: a multi-writer
  update is `UPDATE ... WHERE id = ? AND rev = ?` and a zero-row result means the
  caller lost the race and must re-read. `updated_utc` is display bookkeeping.
- `deleted_utc` is the local tombstone time; it is not the peer's `LAST-MODIFIED`
  and is allowed to differ from it.

### 5.4 Attendees and reminders

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

`event_reminders` is only the **definition** of a `VALARM`. Materialized occurrences,
leases, snooze/dismiss state, and delivery attempts are notification-owned and are
defined in §5.11. A change to either table still bumps the parent event's `rev`, so
the event aggregate remains the sync unit.

### 5.5 Derived recurrence cache (disposable)

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

Invalidation is automatic (triggers, §5.8): any `rev` change on a master or on one of
its overrides deletes the affected cache rows inside the same transaction. Readers
must also verify `master_rev = events.rev` before trusting an instance row.

### 5.6 Change log, batches, cursors, conflicts

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
  scope          TEXT NOT NULL DEFAULT 'profile'
                 CHECK (scope IN ('profile', 'global')),
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
CREATE INDEX idx_change_scope ON change_log(scope, seq);
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
- `seq` is per store and monotonic. With several writers on the global store,
  SQLite's write lock is held from before the first log insert through commit, so
  commit order equals `seq` order. A reader can treat `seq` as a total order **within
  that store**; rollback gaps are normal and cross-store comparison is invalid.
- Every row carries the row's `scope`; it is redundant with the store but makes
  backups/exports self-describing and supports a future one-database migration.
- `origin = 'system'` rows (migrations, maintenance) are never pushed. Triggers mark
  them pushed automatically, and the CHECK refuses a hand-written system row without
  `pushed_utc`, so they cannot appear in `pending_push`.
- `change_context.origin` defaults to `'local'`. That is the safe failure mode: if a
  writer forgets to set the context, a change is (harmlessly) pushed to a peer rather
  than silently withheld. The repository must still set it on every transaction
  (§7.3).
- `delete` rows keep the last known state in `before_json` so a pusher can still
  address a peer resource (UID, href, etag) after the local row is tombstoned or
  purged.
- `op = 'insert'` and `op = 'update'` are both upserts for consumers. Resurrecting a
  tombstoned event logs `update`. Consumers must not assume a strict state machine.
- `before_json`/`after_json` are **row snapshots**, not full aggregates. They carry
  identity and version fields plus the columns that commonly matter. A consumer that
  needs the full event re-reads it by `entity_id`; if the row is gone, the change is
  a deletion. This keeps triggers small and avoids duplicating attendee/reminder JSON
  on every child edit.
- `sync_conflicts` is written by the sync engine (owned by sync design); the core
  provides the table because it must be transactional with the event write.

### 5.7 Indexes and views

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

-- Exactly one global calendar per global store.
CREATE UNIQUE INDEX ux_calendars_single_global
  ON calendars(scope)
  WHERE scope = 'global';

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
SELECT l.scope, l.entity_type, l.entity_id, MAX(l.seq) AS seq
FROM change_log l
WHERE l.origin = 'local' AND l.pushed_utc IS NULL
GROUP BY l.scope, l.entity_type, l.entity_id;
```

### 5.8 Triggers

Triggers are the **normative** change-capture mechanism. Application code must not
write `change_log` directly (except to mark rows pushed). This guarantees that no
write path, including a future migration script, silently misses the log. Writers set
`change_context` as the first statement inside the same `BEGIN IMMEDIATE`
transaction; SQLite permits one writer at a time per database file, so the singleton
context cannot interleave between connections (including several profile processes
writing the global store).

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
7. A source or calendar cannot be inserted with a `scope` that disagrees with
   `schema_meta.store_scope`; changing `scope` in place is rejected.

```sql
-- Guard: the row's scope must match the physical store it lives in.
CREATE TRIGGER trg_sources_scope_guard
BEFORE INSERT ON sources
WHEN NEW.scope <> (SELECT value FROM schema_meta WHERE key = 'store_scope')
BEGIN
  SELECT RAISE(ABORT, 'source scope does not match store scope');
END;

CREATE TRIGGER trg_calendars_scope_guard
BEFORE INSERT ON calendars
WHEN NEW.scope <> (SELECT value FROM schema_meta WHERE key = 'store_scope')
BEGIN
  SELECT RAISE(ABORT, 'calendar scope does not match store scope');
END;

CREATE TRIGGER trg_sources_scope_immutable
BEFORE UPDATE OF scope ON sources
WHEN NEW.scope <> OLD.scope
BEGIN
  SELECT RAISE(ABORT, 'source scope is immutable');
END;

CREATE TRIGGER trg_calendars_scope_immutable
BEFORE UPDATE OF scope ON calendars
WHEN NEW.scope <> OLD.scope
BEGIN
  SELECT RAISE(ABORT, 'calendar scope is immutable');
END;

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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     rev, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', NEW.id, NEW.scope, NEW.calendar_id, 'insert',
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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     rev, before_json, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', NEW.id, NEW.scope, NEW.calendar_id, 'update',
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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     rev, before_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', NEW.id, NEW.scope, NEW.calendar_id, 'delete',
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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     rev, before_json, pushed_utc)
  VALUES (
    unixepoch(), 'event', OLD.id, OLD.scope, OLD.calendar_id, 'delete',
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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     after_json, pushed_utc)
  VALUES (
    unixepoch(), 'calendar', NEW.id, NEW.scope, NEW.id, 'insert',
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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     before_json, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'calendar', NEW.id, NEW.scope, NEW.id, 'update',
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
    (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
     before_json, pushed_utc)
  VALUES (
    unixepoch(), 'calendar', NEW.id, NEW.scope, NEW.id, 'delete',
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
    (at_utc, entity_type, entity_id, scope, op, origin, actor, batch_id,
     after_json, pushed_utc)
  VALUES (
    unixepoch(), 'source', NEW.id, NEW.scope, 'insert',
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
    (at_utc, entity_type, entity_id, scope, op, origin, actor, batch_id,
     before_json, after_json, pushed_utc)
  VALUES (
    unixepoch(), 'source', NEW.id, NEW.scope, 'update',
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
    (at_utc, entity_type, entity_id, scope, op, origin, actor, batch_id,
     before_json, pushed_utc)
  VALUES (
    unixepoch(), 'source', NEW.id, NEW.scope, 'delete',
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

### 5.9 Migrations

- One numbered migration per `schema_version` bump, applied inside a single
  `BEGIN IMMEDIATE` transaction with `change_context.suppress = 1`.
- `PRAGMA user_version` mirrors `schema_meta.schema_version` for fast startup checks.
- **Per-store migrations.** The profile store and the global store migrate
  independently. An older profile build may open the global store after a newer build
  migrated it; use additive migrations (new nullable columns/tables) and a
  `min_reader_version` in `schema_meta` so an old build refuses to write rather than
  corrupting data. Never require every profile to upgrade in lockstep.
- **Initialization race.** Several profiles may open a missing global store at the
  same time. The first `BEGIN IMMEDIATE` creates the schema/seeds the global calendar;
  the others wait on `busy_timeout`, re-check `user_version`, and proceed. Migration
  code must be idempotent under this race and must not assume it is the only starter.
- Migrations that transform event rows must bump `rev` per event, or explicitly
  write `origin='system'` log rows; either way the UI must be told to refresh.
  Preferred: bump `rev`, let triggers log origin `system`, and let the app treat
  `system` changes as refresh-only.
- Schema v3 is data-only: it inserts the default `Personal` calendar into a profile
  store whose `calendars` table is still empty (counting tombstones), through the
  normal change-capture path with `origin='system'`, so the row is logged as
  refresh-only and excluded from `pending_push`. Because migrations run once per
  store, later user deletions are never undone and the global store is untouched.
- Never rewrite `etag` during migration unless the peer version changed; doing so
  would cause spurious `If-Match` failures.
- Migrations must never copy rows between the profile and global stores implicitly.
  Cross-store moves are explicit user actions with intent records (§4.4).

### 5.10 Optional full-text search (v1.1)

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

### 5.11 Notification-owned tables (reminder delivery state)

These tables live in the **per-profile database** and are owned by
`docs/design/app-notifications.md`. They are repeated here because the schema is
shared and because the boundary has to be explicit. They are not part of the event
aggregate and do not trigger `change_log` entries.

Core owns `event_reminders` (the `VALARM` definition). The notification layer owns the
materialized occurrence, its state machine, the lease, and every delivery attempt.
`reminder_instances` uses **soft references** to `events`/`event_reminders` because a
row may refer to an event in the **global store**, which is a different database file;
a foreign key cannot cross that boundary. The notification layer resolves
`(store_id, event_id)` and treats a missing target as cancelled.

```sql
-- Per-profile, notification-owned. Timestamps are epoch seconds UTC.
CREATE TABLE reminder_instances (
  id                  TEXT PRIMARY KEY,
  store_id            TEXT NOT NULL,          -- 'profile:coding' | 'global'
  event_id            TEXT NOT NULL,          -- events.id in that store (soft ref)
  reminder_id         TEXT NOT NULL,          -- event_reminders.id (soft ref)
  recurrence_key      TEXT NOT NULL,          -- core canonical occurrence key
  occurrence_start_utc INTEGER NOT NULL,
  offset_seconds      INTEGER NOT NULL,       -- materialized VALARM offset
  fire_at_utc         INTEGER NOT NULL,
  priority            TEXT NOT NULL DEFAULT 'normal'
                      CHECK (priority IN ('digest', 'normal', 'high', 'critical')),
  channel_policy_json TEXT NOT NULL DEFAULT '{}'
                      CHECK (json_valid(channel_policy_json)),
  state               TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK (state IN ('scheduled', 'firing', 'retrying',
                                       'delivered', 'snoozed', 'acknowledged',
                                       'suppressed', 'cancelled', 'failed',
                                       'dead_letter')),
  attempt             INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  lease_until_utc     INTEGER,
  snooze_until_utc    INTEGER,
  dedup_key           TEXT NOT NULL,
  created_utc         INTEGER NOT NULL,
  updated_utc         INTEGER NOT NULL,
  UNIQUE (store_id, event_id, reminder_id, recurrence_key),
  UNIQUE (dedup_key)
);

CREATE INDEX idx_reminder_instances_due
  ON reminder_instances(state, fire_at_utc);
CREATE INDEX idx_reminder_instances_event
  ON reminder_instances(store_id, event_id);

CREATE TABLE notification_deliveries (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  reminder_instance_id TEXT NOT NULL
                       REFERENCES reminder_instances(id) ON DELETE CASCADE,
  channel              TEXT NOT NULL,   -- in_app|hermes_send|web_push|ntfy|pushover|desktop
  target               TEXT NOT NULL,   -- opaque, allowlisted
  attempt              INTEGER NOT NULL,
  started_utc          INTEGER NOT NULL,
  finished_utc         INTEGER,
  outcome              TEXT NOT NULL
                       CHECK (outcome IN ('success', 'transient', 'permanent', 'unknown')),
  exit_code            INTEGER,
  provider_id          TEXT,
  error                TEXT,            -- redacted
  latency_ms           INTEGER
);

CREATE INDEX idx_notification_deliveries_instance
  ON notification_deliveries(reminder_instance_id);

CREATE TABLE notification_channels (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
  secret_ref  TEXT,
  created_utc INTEGER NOT NULL
);

CREATE TABLE web_push_subscriptions (
  id             TEXT PRIMARY KEY,
  endpoint       TEXT NOT NULL UNIQUE,  -- capability URL, treat as secret
  p256dh         TEXT NOT NULL,
  auth           TEXT NOT NULL,
  user_agent     TEXT,
  created_utc    INTEGER NOT NULL,
  last_success_utc INTEGER,
  failure_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE notification_prefs (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL CHECK (json_valid(value_json)),
  updated_utc INTEGER NOT NULL
) WITHOUT ROWID;
```

Reconciliation with `app-notifications.md` (field conflicts resolved here):

| App-notifications sketch | Core-side resolution |
|---|---|
| `events` mirror table | **Removed.** Read core `events`/`event_instances`; there is one event table per store. |
| `reminders` | Renamed `reminder_instances`; `event_reminders` is the definition, the instance is per occurrence. |
| `reminders.occurrence` (ISO text) | `recurrence_key` from core (`T:<epoch>` / `D:<date>`); `occurrence_start_utc` is an INTEGER. |
| `reminders.offset_sec` | `offset_seconds` (same meaning). |
| `reminders.fire_at_utc` (TEXT) | `fire_at_utc` INTEGER epoch seconds. |
| `created_at` / `updated_at` | `created_utc` / `updated_utc` INTEGER; this matches the core convention for the shared DB. |
| `deliveries` | Renamed `notification_deliveries`; columns kept. |
| `channels`, `prefs` | Renamed `notification_channels`, `notification_prefs` to avoid clashing with core/peer vocabulary. |
| `web_push_subscriptions` | Same name; timestamps converted to `_utc` INTEGER. |
| `priority`, `channel_policy` | Notification-owned, not core event fields. |
| `dedup_key` | Kept; additionally a direct `UNIQUE(store_id, event_id, reminder_id, recurrence_key)` is the real dedupe spine. |

Per-profile ownership: each profile materializes its own `reminder_instances` for
global events as well, using `store_id = 'global'`. This avoids cross-profile
delivery-state contention and lets each profile choose its own channels. The global
store never contains notification tables. If two profiles notify the same human for
the same global event, that is a routing/preferences problem for the notification
layer, not duplicate core data (Q23).

---

## 6. Recurrence handling

### 6.1 Semantics we implement

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

### 6.2 Expansion in local time

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

`rruleIterator` is a generator over local datetimes, not instants. **DECIDED (Q4):**
v1 supports the P0 subset — `FREQ`, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`
(including ordinal prefixes such as `-1FR`), `BYMONTHDAY`, `BYMONTH`, `BYSETPOS`, and
`WKST` — and flags everything else. `BYSECOND`, `BYMINUTE`, `BYHOUR`, `BYYEARDAY`,
`BYWEEKNO`, `RSCALE`, and `SKIP` are stored verbatim but are **not expanded**; the
affected series is surfaced with the unsupported parts listed in
`rrule_unsupported_json`, and the master is shown at its `DTSTART` only. Do not
silently drop the modifiers and expand a different series; that is worse than showing
one instance plus a warning. Use a vetted library for the P0 grammar and its
conformance suite; do not hand-roll it.

Fast-forwarding: for simple rules (`FREQ=DAILY/WEEKLY/MONTHLY/YEARLY` with no
`BYSETPOS`), jump directly to the first candidate at or after `localFrom` instead of
iterating from `DTSTART`. For complex rules, iterate with a hard candidate cap
(default 100,000) and return a warning if exceeded. This keeps "next occurrence of an
event that started in 1998" cheap.

### 6.3 Duration of occurrences

- Timed: `end = start + duration_seconds` (exact instant delta). A 60-minute meeting
  starting at 01:30 local on a fall-back day lasts 60 real minutes, which may display
  as 02:30 local.
- All-day: `end_date = start_date + (master.end_date − master.start_date)` days.
- An override carries its own start/end and does **not** inherit duration from the
  master.
- `DURATION:P1D` on a timed event is normalized to 86,400 seconds at import.
  **DECIDED (Q7):** duration is an exact instant delta (end − start), not a nominal
  wall-clock duration; the original duration form is not preserved. All-day events
  are the exception and stay date-based.

### 6.4 DST correctness

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

### 6.5 Exceptions and overrides

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

### 6.6 `RANGE=THISANDFUTURE`

RFC 5545 defines this as "this and all future instances" with a delta applied to
subsequent occurrences. Client support is uneven and semantics for multiple
overlapping ranges are underspecified. v1 policy:

1. Store the override and its range faithfully.
2. During expansion, apply it only to the occurrence matching its `recurrence_key`.
3. Log a warning (`recurrence_range_unsupported`) once per event.
4. Do not rewrite the master's RRULE.

This is deliberately conservative and may be revisited (§11, Q5).

### 6.7 Caching and invalidation

- **DECIDED (Q8):** the instance cache is optional in v1. Expand on read for the
  requested window; typical personal calendars (<20k events) expand a month in
  single-digit milliseconds. Enable `event_instances` only if profiling exceeds the
  targets in §10 or if the notification layer needs a persistent horizon.
- When `event_instances` is enabled, fill it per series per window; a row is valid
  only if `master_rev = events.rev` and `series_state.tzdb_version` matches the
  current tz database version.
- Triggers invalidate the cache on every `rev` change, so stale rows should be
  impossible; the `master_rev` check is a second line of defense for crashes during
  a rebuild and for restored backups.
- Cache writes never go through `change_context`/change-log triggers because they do
  not modify `events`.

### 6.8 Range-query algorithm

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

### 6.9 Time zone data versioning

- Record the tz database version in `series_state.tzdb_version` (e.g. `2026a`).
- On startup, if the bundled version differs, delete all `series_state` and
  `event_instances` rows (cheap, derived) and bump a UI refresh token.
- Remote `VTIMEZONE` blobs are stored verbatim in `custom_timezones`; if a remote
  later changes a blob, that is a calendar-level change and invalidates affected
  series.
- Never rewrite stored `start_utc` for existing non-recurring events on tzdb update;
  preserve imported instants and only change future expansion.

---

## 7. Change log and sync substrate

### 7.1 What the log guarantees

1. **Per-store total order**: `seq` is unique and increasing within one store. When a
   store has several writer processes (the global store), commit order equals `seq`
   order because only one write transaction can hold the database write lock at a
   time; `seq` is assigned inside that lock. Rolled-back transactions may leave gaps,
   which consumers must treat as normal. Comparing `seq` across stores is meaningless;
   the cross-store cursor is `(store_id, seq)`.
2. **Atomicity**: the data change and its log row(s) commit together or not at all,
   per store. No transaction spans the profile and global stores (§4.4).
3. **Completeness**: triggers capture every `events`/`calendars`/`sources` write,
   including writes made outside the repository, in the store where the write occurs.
4. **Attribution**: every row has `scope`, `origin`, and optional `actor`/`batch_id`.
5. **Durability**: log rows survive until explicitly pruned; deletion tombstones keep
   enough state to address an external peer's object.
6. **Idempotent consumption**: consumers can re-read any range; rows are immutable
   except for push markers.

Non-guarantees (by design): one row per user action (coalescing is allowed), field
level diffs (row snapshots only), or replay to reconstruct history (use backups).

One transaction can emit several rows for the same entity: creating an event with two
attendees and a reminder produces an `insert` plus three `update` rows, one per child
trigger. Consumers coalesce by `(store_id, entity_type, entity_id)`; `pending_push`
already does this inside a store, and the UI should too.

### 7.2 What the app and sync engine do with it

| Consumer | Query | Behavior |
|---|---|---|
| UI / view models | `SELECT ... WHERE seq > cursor` per store | Refresh affected entities; keep one cursor per store in `sync_cursors` or memory. |
| Push worker | `pending_push` + `change_log` | Push latest state per entity in `seq` order, then mark pushed. Only the elected owner pushes a global calendar (§4.4). |
| Pull worker | peer delta | Apply in one tx with `origin='remote'`, pre-marked pushed. |
| Debug/audit | `change_log` by entity | Show history of an event. |
| Snapshot client | `seq > cursor`; if cursor < `min_available_seq` | Full resync of that store. |

### 7.3 Write path

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
leak into another's transaction. This holds for several profile processes writing the
global store. Do **not** set the context outside the transaction. `origin` defaults to
`'local'`; forgetting to set it turns a maintenance write into a harmless extra push
rather than silent data loss, but the repository is still required to set it explicitly
on every transaction. `scope` is implied by the store, but triggers copy it from the
row so the log stays self-describing.

For the global store, every update that can race with another profile uses
compare-and-swap:

```sql
UPDATE events
   SET summary = :summary, rev = rev + 1, updated_utc = unixepoch()
 WHERE id = :id AND rev = :expected_rev;
-- zero rows changed => 409 conflict; re-read and retry
```

`BEGIN IMMEDIATE` serializes the transaction but does not turn a separate read and
write into one atomic check; the `rev` predicate does. If the write holds the lock
longer than `busy_timeout`, other profiles get `SQLITE_BUSY` and must retry with
backoff. Keep transactions short; never do network I/O inside one.

Invariants the repository must uphold:

- Every content change to an event bumps `rev` at least once and sets
  `updated_utc`. (Child tables do this automatically via triggers; direct event
  updates must do it explicitly.)
- Master and overrides are written in the same transaction.
- `events.uid`, `calendar_id`, `source_id`, and `scope` never change in place; use
  move/copy semantics.
- Soft delete sets `deleted_utc`; it does not clear `etag` (the pusher needs it for
  `If-Match`).
- Bulk operations set one `batch_id` for the whole transaction and stay within the
  store; a publish to the global store is a separate, reconciled transaction (§4.4).

### 7.4 Push path

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
not happen while unpushed rows reference the event (§8).

### 7.5 Pull path

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

### 7.6 Conflict handling

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

### 7.7 Retention and snapshots

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

### 7.8 Failure modes and how the design covers them

| Failure | Result |
|---|---|
| Crash after local data write | Log row committed with it; nothing to repair. |
| Crash after remote write, before push marker | Push retried; PUT with same UID/href is idempotent; `If-Match` may 412 → conflict path. |
| Crash mid-pull | Single transaction rolls back including cursors; replay is idempotent. |
| Two profile processes write the global store | One holds the write lock; the other waits up to `busy_timeout`, then retries with backoff; `seq` has no interleaving. |
| Global writer holds the lock too long | Other writers get `SQLITE_BUSY`; WAL grows and checkpoints starve; chunk work and keep transactions short. |
| Global store on a network filesystem | Hermes falls back to DELETE journal mode; readers and writers block each other and locks may be unreliable; use local storage or a single writer. |
| Stale read-modify-write by two profiles | `UPDATE ... WHERE rev = :expected` changes zero rows; caller gets 409 and re-reads. |
| Trigger bypassed by `PRAGMA recursive_triggers=OFF`? | Off by default; our triggers do not recurse into the same table. Do not enable recursive triggers globally. |
| Clock moves backwards | `seq` still monotonic; `rev` unaffected; timestamps may look odd but ordering holds. |
| Remote reuses UIDs across calendars | Unique index is per calendar, so allowed. |
| Remote deletes and re-creates same UID | Upsert by href/uid resurrects the tombstone and logs `update`. |

---

## 8. Lifecycle, purge, and retention

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

## 9. Query patterns

All examples assume UTC epoch seconds and a half-open window `[from, to)`. Every
query runs **inside one store**; a cross-store view runs it twice and merges the
results (§4.5). If a future one-database variant is adopted, add
`AND scope = :store_scope` to each query and include `scope` in the uniqueness keys.

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

-- Reminder definitions for an event; the core expands occurrences, and the
-- notification layer materializes reminder_instances (optional cache shown).
SELECT r.*, i.start_utc, i.recurrence_key
FROM event_instances i
JOIN event_reminders r ON r.event_id = COALESCE(i.override_event_id, i.event_id)
WHERE i.start_utc >= :from AND i.start_utc < :to;

-- Notification layer: due rows, one profile, both stores mixed by store_id.
SELECT * FROM reminder_instances
WHERE state = 'scheduled'
  AND fire_at_utc <= :now
  AND (lease_until_utc IS NULL OR lease_until_utc < :now)
ORDER BY fire_at_utc;

-- Unpushed local work (per store).
SELECT * FROM pending_push ORDER BY seq;

-- Changes since a UI cursor (per store; keep one cursor per store_id).
SELECT * FROM change_log WHERE seq > :cursor ORDER BY seq;
```

Required API surface (language-neutral):

```text
getEvent(storeId, id) -> Event | null
queryRange(fromUtc, toUtc, {calendarIds?, includeAllDay?}) -> Instance[]  # merges stores
nextOccurrence(storeId, eventId, afterUtc) -> Instance | null
createEvent(storeId, input, origin) -> Event
updateEvent(storeId, id, patch, expectedRev) -> Event   # 409 on rev mismatch
deleteEvent(storeId, id, {occurrenceKey?}) -> void
publishToGlobal(profileEventId) -> Event                # copy + intent, §4.4
changesSince(storeId, seq) -> Change[]
pendingPush(storeId, limit) -> PendingChange[]
markPushed(storeId, entityType, entityId, throughSeq)
applyRemote(storeId, change, batchId) -> ApplyResult
remindersDue(profileId, fromUtc, toUtc) -> ReminderInstance[]
```

---

## 10. Testing strategy

### Schema and invariants

- Every CHECK constraint has a test that must fail: all-day without dates, timed
  without zone, floating with zone, end before start, duration mismatch, override
  without key, override with RRULE, duplicate master UID, duplicate override key,
  suppression flag bypass, a profile-scoped row without `owner_profile_id`.
- Composite FK tests: an event whose `source_id` disagrees with its calendar is
  rejected; an event whose `scope` disagrees with its calendar is rejected; a
  calendar/source whose scope disagrees with `schema_meta.store_scope` is rejected by
  the guard triggers; changing `scope` in place is rejected.
- Store identity: a profile store seeded with `store_scope='profile'` cannot insert a
  global calendar, and a global store cannot insert a profile calendar.
- Override guard: wrong calendar, wrong UID, parent is itself an override → rejected.
- Migration test: v1 fixture upgrades to current, `PRAGMA user_version` and
  `schema_meta` agree, all events have `rev >= 1`, and `store_scope`/`store_id` are
  preserved.

### Multi-writer and storage scopes

- Two connections write the global store concurrently with `BEGIN IMMEDIATE`; verify
  `seq` order equals commit order and no rows interleave.
- Hold a write transaction past `busy_timeout`; the second writer gets `SQLITE_BUSY`
  and succeeds on retry with backoff.
- Stale compare-and-swap: two readers fetch `rev=3`, both update; exactly one gets a
  row, the other gets zero rows and a 409 path.
- Cross-store merge: profile + global results sort correctly and are deduped by
  `(store_id, id)`; identical UIDs in both stores remain two rows.
- Publish intent: crash between the profile copy and the global insert leaves a
  replayable intent; replay is idempotent and does not duplicate the global event.
- Global store unavailable: profile-only mode still serves events and raises a visible
  degraded state; no silent write into the profile store.

### Change log

- **No missed writes**: property test that random mutations through the repository
  and through raw SQL both produce exactly one log row (or a coalescible set) per
  event revision.
- **Origin isolation**: remote apply produces log rows with `pushed_utc` set and
  `origin='remote'`; `pending_push` never contains them.
- **Scope isolation**: every log row's `scope` matches its store and entity; a global
  row never appears in a profile `pending_push`.
- **Child edits**: attendee add/remove, reminder add/remove bump `rev` and produce an
  event update log.
- **Delete semantics**: soft delete logs `delete` once (not `update` + `delete`);
  resurrection logs `update`; hard delete logs `delete` unless suppressed.
- **Cursor semantics**: reader sees a consistent prefix; rollback gaps are tolerated;
  `min_available_seq` forces full resync after pruning.
- **Crash simulation**: kill between remote PUT and `markPushed`, verify retry
  idempotence; kill mid-pull, verify rollback.

### Recurrence conformance

- RFC 5545 §3.8.5.3 examples for the **P0 subset** as golden tests; unsupported
  modifiers must be flagged and must not be silently expanded as if absent.
- DST matrix from §6.4.
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

## 11. Open questions and decision log

Each item states the current default so implementation can proceed. Items marked
**DECIDED** or **RESOLVED** are recorded so the decision is not relitigated; the
remaining open items need an owner decision.

**Q1. DECIDED — Hermes is the host, not a calendar peer.**
The plugin **is** the Hermes calendar. Hermes supplies the runtime, profile data
directories, and shared root; it exposes no calendar API and is never synced against.
The local store is the system of record for plugin-created events. External providers
(CalDAV, Google, Graph, ICS) are peers per `docs/design/sync.md`; when a calendar is
linked to one, the peer's accepted representation and `etag` are recorded and the
conflict rules in `sync.md` apply. *Resolved by the owner requirement; no
Hermes-as-calendar-API dependency remains.*

**Q2. DECIDED — etags are peer-only, not a Hermes concept.**
`etag` exists only for external sync peers. Peers with per-resource versions use them
directly; peers that expose only collection ctags get a synthesized hash stored as
`etag`. Local events (`source.kind = 'local'`) always have `etag = NULL` and use `rev`
for local concurrency.

**Q3. Who mints UIDs for local events, and may a UID change?**
*Default:* the core mints a UUIDv7 at create and uses `<uuidv7>@hermes.local` for
outbound UIDs; UID never changes in place. A copy between stores may mint a new UID
when the destination calendar could collide (§4.6). *Risk:* a peer may reject or remap
the UID on first push; the peer's value wins thereafter and is preserved.

**Q4. DECIDED — support the P0 RRULE subset only in v1.**
Supported: `FREQ`, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (with ordinals),
`BYMONTHDAY`, `BYMONTH`, `BYSETPOS`, `WKST`. Everything else (`BYSECOND`,
`BYMINUTE`, `BYHOUR`, `BYYEARDAY`, `BYWEEKNO`, `RSCALE`, `SKIP`, vendor parts) is
preserved verbatim, listed in `rrule_unsupported_json`, and **not expanded**; the
master is shown at `DTSTART` with a visible warning. Do not silently expand a
different series.

**Q5. Do we support `RANGE=THISANDFUTURE` in v1?**
*Default:* store, apply only to the named occurrence, warn. Full semantics (delta
applied to all future instances, interaction with later overrides) is significant
work.

**Q6. Floating (zone-less) timed events: support, normalize, or reject?**
*Default:* support and store; resolve with the calendar default zone for indexing.
*Risk:* reminders and DST behavior are inherently ambiguous.

**Q7. DECIDED — duration is exact seconds, not nominal wall time.**
`duration_seconds = end_utc − start_utc`; a 60-minute event crossing a DST
transition lasts 60 real minutes and may display as a shifted wall-clock range.
All-day events remain date-based. `DURATION:P1D` on a timed event means 86,400
seconds, not "same local time tomorrow." Clients that expect nominal duration are
out of scope for v1.

**Q8. DECIDED — the instance cache is optional in v1.**
Expand on read by default; keep `event_instances` and `series_state` as the specified
optional cache, enabled only when profiling exceeds the §10 targets or the
notification layer needs a persistent horizon. The invalidation triggers and schema
stay so enabling it later is not a migration.

**Q9. RESOLVED — reminder delivery state is notification-owned.**
The core keeps `event_reminders` (the `VALARM` definition). The notification layer
owns `reminder_instances`, `notification_deliveries`, channels, subscriptions,
preferences, lease, snooze, and dismiss in the **per-profile** database. Tables and
field conflicts are reconciled in §5.11. Still open: whether two profiles notifying
the same human about the same global event should dedupe (Q23).

**Q10. How long do we retain the change log and tombstones?**
*Default:* 30 days log, 90 days tombstones per store. Sync design must confirm the
maximum offline window for a client before forcing full resync.

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
*Default:* preserve the peer's `SEQUENCE`; bump it on local edits to events with
attendees or an organizer, leave it otherwise. *Open:* depends on iTIP scope (Q12)
and peer requirements.

**Q19. Is `<get_default_hermes_root()>/plugin-data/<name>/` actually reachable under
`plugins.isolation: host` (and under stricter modes)?**
This is the proposed global-store path and the only unverified storage assumption.
*Default:* use the path, detect open failure, run profile-only with a visible degraded
state, and never silently relocate global data into the profile store. *Needs:* a host
fact from the Hermes/plugin API research, then a deployment note. *Blocking for
shipping the global calendar, not for the profile calendar.*

**Q20. Is profile identity the directory name, and what happens on rename/delete?**
*Default:* `profile:<directory-name>` (`coding`, `local`, `pentest`); a rename is a
migration of the profile store's `store_id`/`owner_profile_id`, not a new identity.
Deleting a profile leaves global rows with a dangling `owner_profile_id`; *open:*
whether to keep the author label, anonymize it, or offer a transfer. *Risk:* two
profiles can be renamed into the same name; the store path collision must be rejected.

**Q21. What exactly does "publish to global" mean?**
*Default:* copy-on-publish: new internal `id`, new UID only if the destination is a
synced calendar or has a UID collision, `owner_profile_id` = publisher, and a
replayable intent row. *Open:* whether later edits on either copy stay linked (a
"published from" pointer), how unpublish/delete propagates, and whether the global
copy is writable by the source profile only or by everyone. The current model allows
everyone to edit; linking copies would need an alias table and a conflict policy.

**Q22. Which process owns external sync for the global calendar?**
*Default:* one elected worker holds a lease row in the global store; other profiles
never push it. *Open:* whether the global calendar is allowed to have an external peer
at all in v1, and how failover works if the owning profile is not running.

**Q23. Do two profiles dedupe notifications for the same global event?**
Each profile materializes its own `reminder_instances`. If profiles belong to the same
human with the same channels, the user could get N notifications. *Default:* no
cross-profile dedupe in v1; document it and make per-profile routing explicit. *Open:*
a shared delivery-claim table in the global store (with its own contention and privacy
questions).

**Q24. Is the shared root guaranteed to be on a local filesystem, and who may write
it?**
WAL is required for normal multi-profile concurrency; Hermes falls back to DELETE on
network storage, where readers block writers and POSIX locks may be unreliable.
*Default:* require local storage; detect the actual journal mode and refuse
multi-profile writes on network storage. *Open:* file ownership/permissions when
different OS users run profiles, and whether a single-writer proxy process is needed
instead of direct multi-process access.

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
| `RRULE` | `rrule` + `rrule_unsupported_json` | Value verbatim; P0 subset expands, everything else flagged. |
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

*End of draft. Q1, Q2, Q4, Q7, Q8, and Q9 are decided in §11. The highest-impact
remaining items are Q19 (global path under isolation), Q20 (profile identity), Q21
(publish semantics), Q22 (global sync ownership), and Q24 (network storage).*
