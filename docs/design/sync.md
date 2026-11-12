# Calendar Sync — Design

**Date:** 2026-11-12
**Status:** Draft
**Branch:** `feature/sync-design` (based on `test`)
**Scope:** External calendar sync for the Hermes calendar plugin: protocol selection,
canonical data model, sync engine, conflict rules, authentication, and MVP order.

---

## 0. TL;DR

| Decision | Recommendation |
|---|---|
| First protocol | **ICS feed import** (read-only, no auth) to prove the model and parser |
| MVP write path | **CalDAV two-way** for iCloud, Fastmail, and Nextcloud — one connector, three providers |
| Google | **Calendar API v3**, not CalDAV, after CalDAV ships |
| Microsoft | **Graph**, after Google |
| ICS feeds | **One-way only** — import (pull) and export (publish). Never two-way |
| Auth | Provider tokens live **only on the backend**, encrypted at rest; the app brokers consent when needed and never holds provider tokens |
| Conflicts | Field-level three-way merge; **remote wins on scheduling fields**, guarded last-writer-wins on text, **delete wins**, losing versions retained for 30 days |
| Overall model | **Local-first cache + outbox; each remote collection is authoritative for its own events** |

The MVP is **ICS import + CalDAV two-way** (6–8 engineer-weeks including the
foundation). Google and Microsoft are fast-follows that reuse the same engine
(another 5–7 weeks). ICS import is deliberately first because it exercises the
iCalendar parser, timezone handling, recurrence expansion, and UI with zero
authentication and zero write-risk.

---

## 1. Scope and assumptions

### 1.1 What this document covers

The plugin gives the Hermes agent a calendar: it can read the user's schedule,
create and edit events, find free time, and subscribe to external feeds. To do
that it must keep a local index of events in sync with external providers.

This document covers:

- the four sync substrates: CalDAV, Google Calendar API, Microsoft Graph, ICS;
- the canonical event model and the mapping from each substrate;
- the sync engine (pull, push, cursors, retries, full resync);
- conflict detection and resolution rules;
- authentication and secret storage;
- the order in which to build the connectors, and what "done" means for each.

It does **not** cover UI layout, visual design, agent prompt design, or the
Hermes gateway plugin runtime. Those belong to the sibling design docs.

### 1.2 Assumptions

These assumptions are load-bearing; if one is wrong, the affected section
changes but the protocol comparison and MVP order still hold.

1. **The plugin has a backend module** that can make outbound HTTPS requests
   and persist state. It runs either inside the Hermes gateway process or as a
   sidecar owned by it. The Android client is a thin consumer of the plugin's
   HTTP/WebSocket API.
2. **One user per gateway profile**, with a small number of calendar accounts
   (1–5). This is a self-hosted personal assistant, not a multi-tenant SaaS.
3. The gateway is reachable from the user's phone over Tailscale, a private
   VPN, or the LAN. It is **not** assumed to have a public HTTPS hostname.
4. The Android app can open a browser/Custom Tab and receive an app redirect.
5. The app is the only first-party client; third-party CalDAV/ICS clients may
   also write to the same calendars.
6. The plugin may be offline or asleep; sync must be resumable and idempotent.
7. Event contents are private. They must not be logged, and must only be sent
   to an LLM when the user's request requires it.

### 1.3 Non-goals for the MVP

- iTIP/iMIP scheduling: sending invitations, processing replies, RSVP.
- Free/busy federation across providers (`freebusy` queries).
- Room/resource booking, delegation, and calendar ACL management.
- `VTODO`, `VJOURNAL`, attachments, and conference-data creation.
- Provider-to-provider mirroring (e.g. iCloud ⇄ Google).
- Google Workspace domain-wide delegation or service accounts.
- Real-time collaborative editing of a single event.
- Multi-user shared gateway tenancy.

Attendee handling is the first post-MVP candidate: agent-created meetings
usually need guests, but adding an attendee causes the provider to email them.
That is a destructive side effect and must not ship without an explicit
confirmation flow. Until then, events are created without attendees.

---

## 2. Sync topology

```
┌──────────────────┐         ┌─────────────────────────────────────────────┐
│ Hermes agent     │         │ Hermes calendar plugin (gateway side)       │
│ tools / prompts  │────────▶│                                             │
└──────────────────┘         │  ┌───────────┐        ┌──────────────────┐  │
                             │  │ Tool API  │        │ Sync engine      │  │
┌──────────────────┐         │  │ read/write│        │ pull/push/merge  │  │
│ Android client   │◀───────▶│  └─────┬─────┘        └────────┬─────────┘  │
│ (Hermes Local)   │ plugin  │        │                       │            │
└──────────────────┘ API     │  ┌─────▼───────────────────────▼─────────┐  │
                             │  │ Canonical store: events, collections,│  │
                             │  │ cursors, outbox, revisions, conflicts│  │
                             │  └─────┬───────────────────────┬─────────┘  │
                             │  ┌─────▼──────┐        ┌───────▼─────────┐  │
                             │  │ Credential │        │ Provider        │  │
                             │  │ vault      │        │ adapters        │  │
                             │  └────────────┘        └───────┬─────────┘  │
                             └────────────────────────────────┼────────────┘
                                                              │
                    CalDAV (iCloud/Fastmail/Nextcloud) · Google · Graph · ICS
```

### 2.1 Local-first, remote-authoritative

The store is a **cache plus an outbox**, not a second source of truth:

- **Reads** (agent tools, UI, free-slot search) hit the local store. This keeps
  the agent fast and usable while offline, and lets it combine accounts.
- **Writes** go to the outbox and are pushed to the owning provider. The
  provider's accepted representation is then read back and used as the new
  base.
- **Each event belongs to exactly one writable collection.** There is no
  bidirectional merge between two providers in the MVP. That avoids the
  classic loop and duplicate problems of calendar mirroring.
- **Remote is authoritative for conflict resolution**, because another client
  may have changed the event and because scheduling fields (time, attendees)
  must not diverge between devices.

Why not proxy reads straight through to providers? Because the agent needs
sub-100 ms tool calls, cross-account free/busy, and a stable view when the
network is down. It also needs a place to detect conflicts; a pure proxy
cannot tell "remote changed" from "local changed".

### 2.2 Deployment variants

The engine and adapters are the same in either variant:

| Variant | Where sync runs | Auth implication |
|---|---|---|
| Gateway plugin (assumed) | Hermes gateway process/sidecar, 24/7 | Refresh tokens belong on the gateway; app brokers consent |
| App-embedded | Android app, WorkManager-driven | Same flows, but background sync is subject to Doze and the gateway may not be running |

If the app is embedded, CalDAV/ICS still work. Google/Microsoft OAuth is
app-brokered, which is already the recommended path for Google. The only real
loss is reliable background sync while the phone is asleep.

---

## 3. Provider comparison

### 3.1 Summary

| | **CalDAV** | **Google Calendar API** | **Microsoft Graph** | **ICS feed** |
|---|---|---|---|---|
| Providers covered | iCloud, Fastmail, Nextcloud, Radicale, Baikal, Synology | Google Calendar only | Outlook.com, Microsoft 365 | Any publisher |
| Auth | HTTP Basic with app password (OAuth on some) | OAuth 2.0, offline refresh token | OAuth 2.0 / Microsoft identity platform | None, or a secret URL |
| Incremental sync | `sync-collection` + `sync-token` (RFC 6578); CTag fallback | `syncToken` | `deltaLink` over a `calendarView` window | None — refetch and diff |
| Push | None standard; poll | Channels (webhooks), expiring, no auto-renew | Subscriptions, ≤7 days (rich ≤1 day) | None; poll |
| Write support | Full `PUT`/`DELETE` with ETag preconditions | Full, ETag preconditions | Full, ETag preconditions | **None** |
| Conflict primitive | `If-Match` → 412 | `If-Match` → 412 (etag) | `If-Match` → 412 | Not applicable |
| Recurrence model | `RRULE` + `RECURRENCE-ID` in iCalendar | Master + instances, `recurringEventId` | `seriesMaster` + `occurrence`/`exception` | `RRULE` in feed |
| Typical auth friction | Low (app password) | High (consent screen, verification, 7-day testing trap) | Medium–high (tenant consent) | None |
| Implementation effort | Medium–high (WebDAV/XML quirks) | Medium | Medium–high (delta windows, subscriptions) | Low |
| Best for | Self-hosted and Apple/Fastmail users | Most consumers | Work accounts | Read-only subscriptions |

### 3.2 CalDAV

CalDAV is WebDAV plus iCalendar: RFC 4791 on top of RFC 4918, with
collection sync in RFC 6578 and iCalendar data in RFC 5545. It is the read/write
programmatic calendar interface for iCloud and Fastmail, the native one for
Nextcloud, and the common denominator for self-hosted servers.

**Discovery.** Four round trips, cached per account:

1. `PROPFIND https://<host>/.well-known/caldav` (Depth: 0) → `current-user-principal`.
2. `PROPFIND <principal>` → `calendar-home-set`.
3. `PROPFIND <calendar-home>` (Depth: 1) → each collection's
   `displayname`, `resourcetype` (`calendar`), `supported-calendar-component-set`,
   `calendar-color`, `current-user-privilege-set`, `sync-token`, `getctag`.
4. Optional `PROPFIND` on each collection for supported reports and limits.

Do not hard-code provider URLs. Accept a server URL and discover from there;
ship presets for the common hosts but let the user override. Self-hosted
Nextcloud and reverse proxies frequently use a sub-path and a custom CA.

**Auth.** HTTP Basic over TLS with an app-specific password. iCloud *requires*
an app-specific password (and therefore 2FA). Fastmail supports app passwords
and OAuth for native clients; an app password is fine for a personal
deployment. Nextcloud's CalDAV endpoint is normally used with a username plus
an app password; OAuth2/OIDC is not a reliable CalDAV path, and an app password
also bypasses MFA. Google's CalDAV endpoint speaks OAuth only, but we are not
using it (see §3.3). A generic connector therefore needs pluggable
authentication: Basic (app password) in the MVP, OAuth Bearer later for
Fastmail.

**Incremental sync.** RFC 6578:

- Initial: `REPORT` with an empty `<sync-token/>`, `sync-level 1`, requesting
  `getetag` (and optionally `calendar-data`).
- Store the returned `DAV:sync-token`.
- Later: send the token and receive changed/added/removed hrefs with new ETags.
- An invalid or expired token returns `403` with the
  `DAV:valid-sync-token` precondition. Fall back to a full collection sync.
- Servers may truncate results (`507` with
  `DAV:number-of-matches-within-limits`). Retry with a narrower
  `calendar-query` time range or paginate with `DAV:limit`.
- If a server does not support `sync-collection`, fall back to CTag
  (`getctag` changes → full `calendar-query` diff; CTag is a de facto
  extension, not an RFC). Some servers advertise `sync-collection` but
  implement it poorly; detect and downgrade per collection.

The sync report gives ETags. Fetch event bodies with a `calendar-multiget`
`REPORT` in batches (e.g. 50–100 hrefs) rather than one GET per event. Verify
that the ETag returned by the multiget matches the one in the sync report; if
it changed in between, process the newer body and let the next round settle.

**Write.** A collection is a set of resources addressed by URL. Event identity
is the `UID` inside the iCalendar object, not the filename.

- Create: `PUT <collection>/<uid>.ics` with `If-None-Match: *`.
- Update: `PUT` with `If-Match: "<etag>"`; `412 Precondition Failed` means
  someone else wrote first.
- Delete: `DELETE` with `If-Match`; `404` on a delete is success.
- Use a stable filename derived from the UID so retries are idempotent.
- Preserve unknown properties (`X-*`, `VTIMEZONE`, custom components) when
  round-tripping. Strip only properties the plugin owns.

**Provider quirks.**

| Provider | Endpoint | Auth | Notes |
|---|---|---|---|
| iCloud | `https://caldav.icloud.com/` | Apple ID + app-specific password | 2FA required to create the password; shared calendars appear as separate collections; rate-limits aggressive clients; some collections are read-only |
| Fastmail | `https://caldav.fastmail.com/` | App password, or OAuth for native clients | JMAP does not expose calendars; CalDAV is the only API; OAuth registration is not required for native public clients |
| Nextcloud | `https://<host>/remote.php/dav/` | Username + app password | No OAuth for CalDAV; self-signed CA common; shared calendars and delegation vary by version; SabreDAV quirks around `sync-token` |
| Radicale / Baikal | Self-hosted | Basic | Scheduling may be absent; no CTag on old versions |
| Google CalDAV | `https://apidata.googleusercontent.com/caldav/v2/` | OAuth 2.0 only | Do not target: OAuth-only, odd endpoint shape, shared quota with the Calendar API, and the API is the better surface |

**Assessment.** One connector buys three of the most common non-Google
providers. There is no app-review process. The cost is XML/WebDAV edge cases
and per-server behavior, which is why the adapter needs a conformance suite
rather than provider-specific branching.

### 3.3 Google Calendar API v3

**Auth.** OAuth 2.0. The supported Android flow is Google Identity's
`AuthorizationClient`, not a custom URI scheme (unsupported on Android) or a
loopback redirect (deprecated for Android). The app calls `authorize()` with
`requestOfflineAccess(serverClientId)` and sends the resulting
`serverAuthCode` to the gateway. The gateway exchanges it at
`https://oauth2.googleapis.com/token` using the Web-application client ID and
secret, and stores the refresh token. This is Google's documented
"maintain ongoing access" flow for native apps with a backend.

Scopes: `calendar.readonly` (list calendars and read) plus `calendar.events`
(read/write events). Incremental authorization is not supported for installed
apps, so request both at once and handle the case where the user grants only
one. Google calls both scopes *sensitive*: public distribution requires
verification, and an app in **Testing** publishing status gets refresh tokens
that expire after **7 days** (and a 100-test-user cap). A self-hosted personal
deployment can run unverified but should be set to **In production** with the
unverified-app warning, otherwise the 7-day expiry breaks 24/7 sync. Unverified
production apps are still capped at 100 users, which is fine for personal use
and a blocker for distribution.

**Incremental sync.** `events.list` with `syncToken`:

- First call: full list (optionally bounded by `timeMin`/`timeMax`) →
  `nextSyncToken` on the last page.
- Later: pass `syncToken`; deletions come back as entries with
  `status: "cancelled"`.
- Paginate with `nextPageToken` until the final page, which carries the new
  `nextSyncToken`.
- An invalidated token returns **410 Gone**; wipe the collection and full-sync.
- Only certain query parameters are allowed with a sync token; use the same
  parameters for every page of a round.

**Push.** `events.watch` creates a channel to an HTTPS webhook. Constraints:

- The callback must have a valid publicly trusted certificate; self-signed
  certs are rejected. A Tailscale/LAN gateway will usually need a relay or
  must fall back to polling.
- Channels expire; there is no automatic renewal. Re-watch with a new channel
  ID before expiry, and tolerate an overlap.
- Notifications carry no event data, only headers. Treat them as "sync now".
- Delivery is explicitly not 100% reliable; a periodic poll is mandatory even
  with push.

For a private-network gateway, **polling is the default** and push is an
optional accelerator where a public HTTPS endpoint exists. A 5–15 minute poll
with jitter is well within quota for one user.

**Limits.** 10,000 requests/minute/project, 600/minute/user/project, and a
1,000,000 requests/day/project billing threshold as of the May 2026 Workspace
quota changes. A single user polling every 15 minutes is nowhere near these.
Randomize poll times by ±25% to avoid synchronized bursts.

**Write semantics.** Create with a client-supplied event `id` (base32hex) so a
retried create is idempotent. Update with `If-Match: <etag>`; `412` means
refetch and merge. Store our local UUID in
`extendedProperties.private.hermes_id` for reconciliation. Google returns
recurring events as a master plus instances when `singleEvents=true`;
single-instance edits appear as separate events with `recurringEventId` and
`originalStartTime`, and cancelled instances have `status: "cancelled"`.

**Note on Google's Calendar MCP server.** Google now exposes a hosted Calendar
MCP server with `list_events`, `create_event`, `suggest_time`, etc. It is a
useful shortcut for an agent that has no calendar store, but it is not a sync
substrate: it has no cursor, no local index, no arbitrary field writeback, no
cross-provider view, and it consumes Workspace quota. Do not build the plugin
on it.

### 3.4 Microsoft Graph

**Auth.** Microsoft identity platform, OAuth 2.0. Delegated scopes
`Calendars.ReadWrite` and `offline_access` (plus `User.Read` if the account
email is needed). Personal Microsoft accounts and work/school accounts both
work; `common` tenant covers both, but organizational accounts may require
admin consent for the app.

Two viable flows for a self-hosted gateway:

1. **Gateway-brokered web OAuth** with PKCE and `state`, callback on the
   gateway's HTTPS origin. Cleanest when the gateway is browser-reachable
   (Tailscale + a trusted certificate).
2. **Device code flow**: the gateway requests a code, shows the URL and code
   in the app, the user approves on their phone or laptop, and the gateway
   polls for the refresh token. This is the reliable fallback when the gateway
   has no browser-reachable HTTPS origin.

Do not embed a WebView. Use the system browser/Custom Tabs.

**Incremental sync.** `GET /me/calendarView/delta?startDateTime=…&endDateTime=…`.

- The initial round returns pages (`@odata.nextLink`) and ends with
  `@odata.deltaLink`.
- Later rounds replay the `deltaLink`; deletions appear under `@removed`.
- The delta is scoped to the **calendarView window**. Changes to events outside
  the window can surface as `@removed` entries; filter carefully. Advance the
  window periodically (e.g. roll it forward daily) and do a full sync when the
  window moves far enough that continuity is unclear.
- `deltaLink` expiry requires a fresh full round.

**Push.** `POST /subscriptions` for `/me/events`:

- Maximum lifetime is **under 7 days** (10,080 minutes); rich notifications
  are **under 1 day** (1,440 minutes). Renew before expiry.
- The endpoint must answer the validation handshake with the `validationToken`
  as `text/plain`.
- Subscribe to lifecycle notifications (`reauthorizationRequired`, `missed`);
  a dropped lifecycle event does not stop sync permanently, but it does pause
  notifications until the subscription is reauthorized.
- Limit is 1,000 active subscriptions per mailbox across all apps.
- Expect throttling (`429` with `Retry-After`) and occasional silent
  subscription loss. As with Google, **poll as the floor**, push as the
  accelerator.

**Write semantics.** `POST /me/events`, `PATCH /me/events/{id}`,
`DELETE /me/events/{id}`. Use `If-Match` with `@odata.etag` on update/delete;
`412` means refetch and merge. Recurrence is modeled as `seriesMaster` plus
`occurrence`/`exception` items (`seriesMasterId`, `type`). `iCalUId` is stable
across a mailbox but read-only. For idempotent creates, use a stable marker in
an open extension or single-value extended property and reconcile by that
marker after an ambiguous failure; do not assume a request that timed out did
not create the event.

**Exchange Online does not expose a supported CalDAV endpoint.** Graph is the
only supported path for Microsoft 365, which is why it gets its own adapter.

### 3.5 ICS feed import/export

ICS is a file format, not a sync protocol. It can be pulled from an HTTP(S)
URL, `webcal://` (rewrite to HTTPS), a local file, or pasted text. It has no
write path, no ETags on events, and no per-event identity beyond `UID`.

**Import.**

- Fetch with `If-None-Match` / `If-Modified-Since` when the server supports it;
  otherwise compare a hash of the body. Honor `REFRESH-INTERVAL` (RFC 7986)
  when present, clamped to a sane range (e.g. 15 minutes to 24 hours).
- Enforce limits: body size (e.g. 10 MB compressed), event count (e.g. 20,000),
  redirect count, and total time. Reject `text/html` and non-calendar content.
- **SSRF protection**: resolve the host, reject private/link-local/loopback
  addresses unless the user explicitly allows a local feed, re-check after
  redirects, and never follow a redirect to a different scheme.
- Treat the URL as a **bearer secret**. Google/Outlook "private" feed URLs
  embed a token; redact them in logs, never return them in full from the API,
  and encrypt them at rest.
- Parse `VEVENT` only. Expand `VTIMEZONE` and `TZID`; handle floating times by
  interpreting them in the feed's display timezone and flagging the ambiguity.
- Feeds are snapshots. Compute a diff against the previous parse: new UIDs are
  inserts, changed content hashes are updates, missing UIDs are deletes. Do not
  delete on a failed or partial parse.
- Duplicate `UID`s across feeds are allowed; identity is (feed, UID).
- Feeds sometimes carry `STATUS:CANCELLED`; treat those as deleted but keep the
  tombstone.
- Feed events are read-only. Any local edit is a **detached copy** in a local
  collection, never a writeback.

**Export.**

- Publish a read-only `.ics` at an unguessable URL (or return a file download).
  Include `PRODID`, `CALSCALE:GREGORIAN`, `X-WR-CALNAME`,
  `REFRESH-INTERVAL`, and proper `VTIMEZONE` blocks.
- Stable `UID`s per event so subscribers update rather than duplicate.
- Token rotation and revocation. Anyone with the URL can read the calendar.
- Optionally export a single event as an invite-shaped `.ics`
  (`METHOD:PUBLISH`, no attendee responses).
- Never accept writes on the export URL.

**Never two-way.** An ICS feed cannot express the preconditions needed for
safe writeback, and feeds commonly strip or rewrite `UID`s. Treat ICS as a
one-way import and a one-way export.

### 3.6 What each connector buys

| Connector | Providers reached | Engine reuse | Auth cost | Relative effort |
|---|---|---|---|---|
| ICS import | Any publisher, including Google/Outlook private feeds | Parser, model, recurrence, UI | None | 1–1.5 wk |
| CalDAV | iCloud, Fastmail, Nextcloud, self-hosted | + cursors, ETags, outbox, conflicts | Low | 3–4 wk |
| Google | Google Calendar | + OAuth broker, syncToken, push | High | 2–3 wk |
| Graph | Outlook.com, Microsoft 365 | + OAuth broker, delta windows, subscriptions | Medium–high | 3–4 wk |

Effort assumes one engineer familiar with the codebase, including tests, and
excludes UI polish.

---

## 4. Canonical data model

### 4.1 Entities

| Entity | Key fields | Notes |
|---|---|---|
| `Account` | `id`, `provider`, `display_name`, `auth_ref`, `base_url`, `status`, `default_tz`, `sync_interval` | `auth_ref` points into the credential vault; never the secret itself |
| `Collection` | `id`, `account_id`, `remote_id`, `name`, `role`, `writable`, `color`, `sync_enabled`, `cursor`, `ctag`, `window_start`, `window_end` | A calendar or feed. `role` = primary, shared_readonly, feed, local |
| `Event` | `id`, `collection_id`, `uid`, `remote_id`, `etag`, `kind`, `status`, `start`, `end`, `all_day`, `tzid`, `rrule`, `exdate`, `recurrence_id`, `summary`, `description`, `location`, `organizer`, `attendees`, `reminders`, `transparency`, `visibility`, `sequence`, `remote_updated`, `local_edited_at`, `local_rev`, `remote_rev`, `content_hash`, `origin`, `deleted_at` | `kind` = single, master, exception; `local_edited_at` is set when a local edit is staged |
| `OutboxOp` | `id`, `event_id`, `op`, `base_etag`, `base_rev`, `payload_hash`, `idempotency_key`, `attempts`, `next_attempt_at`, `state` | Coalesced per event; ordered |
| `Conflict` | `id`, `event_id`, `base_snapshot`, `local_snapshot`, `remote_snapshot`, `fields`, `state`, `created_at` | `state` = open, auto_resolved, user_resolved |
| `Revision` | `id`, `event_id`, `rev`, `snapshot`, `actor`, `created_at` | 30-day retention; supports undo |
| `Feed` | `id`, `collection_id`, `url_encrypted`, `etag`, `last_modified`, `content_hash`, `refresh_interval`, `last_fetch_at` | URL is a secret |

All timestamps are stored in UTC. All-day events are stored as dates with no
timezone. The original `TZID` is preserved for round-tripping and display.

### 4.2 Identity

- **Canonical identity is `(collection, local_uid)`.** For CalDAV/ICS the local
  UID is the iCalendar `UID`; for Google/Graph it is a generated UUID, with the
  provider ID stored as a separate unique attribute.
- Generated events use UUIDv4 `UID`s in our own namespace
  (`<uuid>@hermes.local`) so they cannot collide with a remote event.
- Google: store the provider `id` and our local UUID in
  `extendedProperties.private.hermes_id`. Graph: store `iCalUId` as a
  secondary dedupe key, and our UUID in an open extension.
- Never reuse a `UID` after deletion. Tombstones keep the mapping for at least
  90 days so a late remote echo does not resurrect the event.
- A resource URL is not identity: CalDAV servers may move resources, and
  Google/Graph IDs can change when an event moves between calendars.

### 4.3 Time and recurrence

- **Timed events:** UTC instants plus the original IANA `TZID`. Do not store
  local wall-clock times as the source of truth.
- **All-day events:** `DATE` values, not midnight instants. An all-day event on
  2026-11-12 is a date range, not 00:00–00:00 in a timezone.
- **Floating times:** no `TZID` in the source. Interpret in the collection's
  default timezone, mark `floating=true`, and do not push floating times to
  providers that require a timezone without explicit user confirmation.
- **Duration:** accept `DURATION` on import, normalize to `DTEND`/`end`, but
  preserve `DURATION` if the source used it and the provider prefers it.
- **DST:** expand recurrences in the event's timezone, not UTC. A weekly 09:00
  event stays at 09:00 local across a DST boundary. Test both US and EU
  transitions and a southern-hemisphere zone.
- **Recurrence:** a master (`RRULE`, `RDATE`, `EXDATE`) plus exception rows
  keyed by `RECURRENCE-ID`. Expansion is bounded (default -1 year to +2 years,
  max 750 instances per event) to avoid a runaway infinite series.
- **Series edits** must carry an explicit scope: `this`, `this_and_future`, or
  `all`. Map to the provider's semantics; never guess. Google and Graph model
  "this and future" as a split, which is a series replacement, not a field
  merge.

### 4.4 Field mapping

| Canonical | CalDAV (iCalendar) | Google | Graph | ICS import |
|---|---|---|---|---|
| `uid` | `UID` | `id` + `iCalUID` | `id` + `iCalUId` | `UID` |
| `summary` | `SUMMARY` | `summary` | `subject` | `SUMMARY` |
| `description` | `DESCRIPTION` | `description` | `body.content` (HTML) | `DESCRIPTION` |
| `location` | `LOCATION` | `location` | `location.displayName` | `LOCATION` |
| `start` / `end` | `DTSTART` / `DTEND` | `start.dateTime` + `timeZone` | `start.dateTime` + `timeZone` | `DTSTART` / `DTEND` |
| all-day | `VALUE=DATE` | `start.date` | `isAllDay` | `VALUE=DATE` |
| recurrence | `RRULE`, `RDATE`, `EXDATE` | `recurrence[]` | `recurrence` + `type` | `RRULE`, `RDATE`, `EXDATE` |
| exception | `RECURRENCE-ID` | `originalStartTime` + `recurringEventId` | `seriesMasterId` + `type=exception` | `RECURRENCE-ID` |
| status | `STATUS` | `status` | `isCancelled` + `showAs` | `STATUS` |
| organizer | `ORGANIZER` | `organizer` | `organizer` | `ORGANIZER` |
| attendees | `ATTENDEE` | `attendees` | `attendees` | `ATTENDEE` (read-only) |
| reminders | `VALARM` | `reminders` | `isReminderOn`, `reminderMinutesBeforeStart` | `VALARM` |
| privacy | `CLASS` | `visibility` | `sensitivity` | `CLASS` |
| busy/free | `TRANSP` | `transparency` | `showAs` | `TRANSP` |
| color/category | `CATEGORIES`, `COLOR` | plugin metadata only | `categories` | `CATEGORIES` |
| version | `SEQUENCE`, `LAST-MODIFIED`, `DTSTAMP` | `sequence`, `updated`, `etag` | `changeKey`, `lastModifiedDateTime`, `@odata.etag` | `SEQUENCE`, `LAST-MODIFIED` |

Google has no first-class event category field. Store plugin-only tags in our
metadata, and do not pretend they sync. Graph `body` may be HTML; sanitize on
display and convert to plain text for the agent.

### 4.5 Write set for the MVP

Even on a two-way connector, only a subset of fields is writable by the agent:

| Field | Agent write? | Notes |
|---|---|---|
| summary, description, location | Yes | Plain text; truncate to provider limits |
| start, end, all-day | Yes | Timezone required for timed events |
| recurrence | Yes, with explicit scope | Restricted RRULE subset (`DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`, `COUNT`, `UNTIL`, `BYDAY`) |
| reminders | Local only | Do not push per-event alarms in the MVP |
| transparency / visibility | Yes | Low risk |
| attendees / organizer | **No** | Causes provider emails and RSVP churn |
| attachments / conference data | **No** | Provider-specific; post-MVP |
| color / categories | Local metadata | Do not write remote |
| delete | Yes, with confirmation | Never from an automatic/agent loop without approval |

A provider adapter exposes capabilities, and the tool layer validates the write
against them. Never let the agent write a field the adapter cannot round-trip.

---

## 5. Sync engine

### 5.1 Cycle

One collection is synced by one worker at a time. The cycle is:

```
sync(collection):
    acquire collection lock
    if cursor is null or needs_full:
        full_pull(collection)
    else:
        try:
            incremental_pull(collection)
        except InvalidCursor:
            full_pull(collection)
    if collection.writable:
        drain_outbox(collection)
    release lock
    publish change notifications to app
```

Pull before push. This reduces the chance of pushing a stale edit, and it
means the outbox is merged against the freshest remote state. A push failure
must never block the next pull.

### 5.2 Pull

For each changed resource:

1. Parse the remote representation into a `RemoteSnapshot`.
2. Upsert the remote snapshot into `base`.
3. If the local record is clean, fast-forward the canonical event.
4. If the local record is dirty, run the conflict merge (§6) and either
   resolve automatically or open a `Conflict`.
5. Apply deletions as tombstones, not hard deletes.

Initial full sync is paginated and resumable. Persist the page cursor so a
restart does not re-download everything. For CalDAV, repeat the sync report
until it returns no changes; for Google, page until `nextSyncToken`; for
Graph, page until `deltaLink`; for ICS, one fetch and a diff.

### 5.3 Push

Outbox operations are coalesced per event. If an event has three edits queued,
push the latest state once, not three times. Operations are ordered per event;
different events can proceed in parallel within the account's rate limit.

For each operation:

1. If the event has no remote ID, create it using the provider's idempotent
   create mechanism (§5.6).
2. Otherwise update/delete with `If-Match` on the base ETag.
3. On success, store the new ETag/version and clear the operation.
4. On `412`/`409`, refetch, re-merge from the same base, and retry (max 3,
   jittered). If it still fails, park the event in the conflict queue.
5. On `404` for an update, treat it as remote-deleted → §6.
6. On `410`/invalid cursor, full resync the collection before retrying.
7. On `429`/`503`, honor `Retry-After` and back off exponentially with jitter.
8. On `401`, refresh the token once, then mark the account `reauth_required`.

A push is only considered done when the provider accepts it. The local record
keeps a `pending` marker until the next pull confirms the accepted
representation, which also catches provider-side normalization (e.g. Google
rewriting a timezone).

### 5.4 Cursors and full resync

| Provider | Cursor | Full resync trigger |
|---|---|---|
| CalDAV | `DAV:sync-token` per collection; `getctag` fallback | 403 `valid-sync-token`, missing token, server says so |
| Google | `nextSyncToken` | 410 Gone |
| Graph | `@odata.deltaLink` | expired/invalid delta token, window reset |
| ICS | none | every fetch is a full snapshot |

A full resync must be **safe to run at any time**: it rebuilds the remote
baseline, preserves local dirty events, and re-merges them. It is also the
repair tool for a corrupted cursor. Track `last_full_sync_at` and schedule a
preventive full sync (e.g. weekly, jittered) for providers whose cursors are
opaque and can silently miss changes.

### 5.5 Scope window

Sync only a rolling window by default: **-1 year to +2 years**, configurable
per account. This bounds storage and initial sync time. The window matters for
Graph because `calendarView` delta is windowed. When the window moves:

- Widen it forward in small steps and run a delta for the new slice.
- Never assume that an event outside the window is deleted. Only tombstones
  from the provider are deletions.
- Keep events that fall out of the window until they are older than the
  retention policy, so the agent can answer "when did I last…" questions.

### 5.6 Idempotency and loop prevention

- **Content hash**: hash the canonical fields that are safe to compare, ignoring
  volatile fields (`DTSTAMP`, `LAST-MODIFIED`, `SEQUENCE`, provider `etag`).
  If the hash matches the last synced hash, do not push.
- **Echo suppression**: after a successful push, remember `(remote_id,
  content_hash)` for 60 seconds and ignore a pull that returns exactly that
  state. This is an optimization, not a correctness mechanism; the hash check
  is the real guard.
- **Stable create keys**: deterministic UID/filename for CalDAV, client event
  ID for Google, extension marker for Graph.
- **Single writer per collection** prevents two workers from pushing the same
  event concurrently.
- **No provider-to-provider mirroring** in the MVP. If it is added later, it
  needs an explicit origin tag and a loop budget, and must never mirror
  deletions blindly.

### 5.7 Scheduling

- Poll interval: 5 minutes for the primary calendar when the app is active,
  15–30 minutes in the background, ±25% jitter.
- Push/webhook, when available, only triggers an early sync; never trust it as
  the only signal.
- Backoff: exponential from 30 seconds to 1 hour with jitter. A failing account
  must not starve healthy accounts; use per-account queues and a circuit
  breaker.
- Respect provider maintenance windows and `Retry-After`; do not retry
  non-idempotent creates on timeout without reconciling first.
- Batch where the provider supports it (CalDAV `calendar-multiget`; check
  Google's current batch support before relying on it) and cap batch size so a
  failure is isolatable.

### 5.8 Error taxonomy

| Condition | Meaning | Action |
|---|---|---|
| `401` | Token expired/revoked | Refresh once; then `reauth_required` and notify user |
| `403` | Scope/ACL/permission | Mark collection read-only or account error; do not retry blindly |
| `404` | Resource gone | Treat as remote delete; tombstone |
| `409` | Write conflict / UID collision | Refetch and merge; do not overwrite |
| `410` | Cursor invalid | Full resync |
| `412` | ETag precondition failed | Refetch and merge; retry ≤3 |
| `423` | Locked (Graph/WebDAV) | Back off, retry once, then park |
| `429` / `503` | Throttled/unavailable | Honor `Retry-After`; exponential backoff |
| Parse error | Malformed iCalendar/JSON | Quarantine the item, keep the collection healthy, surface a warning |
| TLS error | Cert/host mismatch | Do not downgrade; ask the user to trust the CA explicitly |

Never silently drop a failed write. The outbox entry stays until it succeeds,
is superseded, or the user discards it.

### 5.9 Implementation notes

- **Do not hand-roll iCalendar or `RRULE`.** Use a mature parser/expander and
  wrap it in the conformance suite from §10.1. Recurrence, DST, and
  `VTIMEZONE` are a bug farm; the library is the cheapest risk reduction in the
  project.
- **CalDAV:** a thin WebDAV/XML layer over the shared HTTP client is enough.
  Do not adopt a full DAV framework; it will fight the sync engine's cursor and
  retry model.
- **OAuth:** use the platform SDKs where they exist (Google Identity
  `AuthorizationClient`, MSAL for Microsoft web/device flows). Do not hand-roll
  token refresh or browser handling.
- **HTTP:** one client with explicit timeouts, redirect policy, proxy support,
  per-account custom CA, and redacted request logging. Every adapter shares it.
- **Storage:** SQLite is sufficient for a single-user gateway; add an FTS index
  for local search. Keep the vault separate from the event database.

---

## 6. Conflict rules

### 6.1 Principles

1. **No silent data loss.** Every losing version is recoverable for 30 days.
2. **Deterministic.** The same inputs always produce the same winner; no
   "random" tie-breaks, no dependence on local clock.
3. **Remote-authoritative for scheduling.** Time, recurrence, organizer, and
   attendees belong to the provider; a local agent must not create a divergent
   meeting.
4. **Minimal surprise.** Merge non-overlapping fields; only escalate actual
   overlaps.
5. **Auditable.** Every automatic resolution records base/local/remote and the
   rule that fired.
6. **The agent is not special.** An agent edit is just a local edit; it gets no
   priority over a human edit from another client.

### 6.2 Definitions

- **Base**: the last remote representation the plugin accepted and synced.
- **Local**: the current canonical record, possibly dirty.
- **Remote**: the representation just fetched.
- **Dirty field**: a field whose local value differs from base.
- **Changed field**: a field whose remote value differs from base.

### 6.3 Decision table

| Local | Remote | Result |
|---|---|---|
| clean | unchanged | No-op |
| clean | changed | Fast-forward to remote |
| dirty | unchanged (ETag matches base) | Push local |
| dirty | changed | Field-level merge; conflict only on overlapping fields |
| deleted | unchanged | Push delete |
| deleted | changed | **Delete wins**; keep the remote edit in `Revision`; offer "restore as new event" |
| changed | deleted | **Delete wins**; keep the local edit in `Revision`; offer "restore as new event" |
| deleted | deleted | Drop; keep a tombstone |
| created locally | same UID exists remotely | Merge as update; keep the older `created` time; flag duplicate |

### 6.4 Field policy

| Field class | Fields | Rule |
|---|---|---|
| Scheduling | `start`, `end`, `all-day`, `rrule`, `exdate`, `recurrence_id`, `status` | **Remote wins** whenever both sides changed |
| People | `organizer`, `attendees` | **Remote wins on conflict**; local attendee edits are rejected in the MVP |
| Descriptive | `summary`, `description`, `location` | Last-writer-wins between `local_edited_at` and the remote `lastModified`, with a 5-second skew guard; tie or missing → remote |
| Local-only | `reminders`, color, plugin tags, local notes | **Local wins**; never pushed, never conflicted |
| Display | `transparency`, `visibility` | Remote wins on conflict |
| Structural | event kind, collection membership, UID | Remote wins; a move is a delete+create unless the provider models it |

Last-writer-wins is used only for descriptive text and only when both sides
changed the same field. Compare the local edit time (`local_edited_at`,
captured when the edit was staged) with the provider's modification timestamp
(`LAST-MODIFIED`/`DTSTAMP`, Google `updated`, Graph `lastModifiedDateTime`).
If the local edit is more than 5 seconds newer, local wins; otherwise remote
wins. If the gateway's clock differs from the provider's HTTP `Date` header by
more than 60 seconds, treat local timestamps as untrusted and let remote win.
The guard tolerates modest clock skew and never lets an uncertain local clock
override a provider write. Scheduling fields never use local time.

### 6.5 Recurring events

- Treat the master and each exception as separate conflict domains.
- A conflict on one occurrence must not rewrite the master.
- Structural changes to a series (new `RRULE`, split, "this and future") are
  **series replacement**, not a field merge. Remote wins; the local version is
  preserved as a revision.
- An exception whose master disappeared is an orphan: keep it locally, mark it
  read-only, and surface it for cleanup. Do not delete user data because a
  provider is inconsistent.
- `EXDATE` and `RECURRENCE-ID` are compared by the occurrence's original start
  in the event's timezone, not by UTC instant alone.

### 6.6 Deletions

Deletion is the most destructive operation and the hardest to merge:

- A delete on either side wins over an edit on the other side.
- The losing content is always retained in `Revision` for 30 days.
- A remote delete followed by a local edit must **not** recreate the event
  automatically. Offer "restore as new event" in the UI; the agent may only do
  it with explicit user confirmation.
- A `404` on update, a Google `status: cancelled`, a Graph `@removed`, or a
  CalDAV href removal are all deletes.
- Tombstones persist for 90 days to absorb late echoes, then are purged.

### 6.7 Precondition failures

`412` is normal, not an error. The sequence is:

1. Refetch the event.
2. Recompute the three-way merge from the same base.
3. Retry the push with the new ETag, up to 3 times with jitter.
4. If still failing, open a `Conflict` and stop automatic retries for that
   event. The rest of the collection continues.

This must be idempotent: a retry after a timeout must first check whether the
previous write actually landed (by remote ID or marker) before creating a
duplicate.

### 6.8 Worked examples

| Scenario | Outcome |
|---|---|
| Agent moves an event 14:00→15:00; another client edits the description | Both apply: time from local, description from remote |
| Agent edits the title; another client moves the event | Title from local, time from remote; no conflict prompt |
| Both move the event to different times | Remote time wins; local time kept in revision; conflict notice |
| Agent deletes; another client adds an attendee | Delete wins; revision retains the attendee edit; user can restore |
| Both add different attendees | Remote attendee list wins; local additions are not sent |
| Agent edits one occurrence; another client edits the master | Occurrence edit and master edit both apply; no series rewrite |
| Two clients create the same UID | Merge, keep the older creation, flag the duplicate, never create a second event |
| Feed event disappears | Tombstone the feed event; do not touch local copies |

### 6.9 Conflict UX

Automatic resolution is silent only when it is provably safe (non-overlapping
fields). Overlapping-field conflicts appear in a **conflict inbox** with:

- a field-by-field diff;
- "keep local", "keep remote", "keep both" (duplicate as a new event);
- a link to the provider's web UI;
- the rule that fired and the timestamps used.

The agent must not resolve destructive conflicts on its own. It can report
"there are 2 conflicts" and, with user approval, apply a chosen resolution.
Every resolution is written to the audit log.

---

## 7. Authentication

### 7.1 Threat model

| Threat | Mitigation |
|---|---|
| Gateway compromise exposes calendar data | Encrypt tokens at rest with a key outside the database; least-privilege scopes; per-account revocation |
| App compromise steals provider tokens | Provider tokens never leave the gateway; the app gets a short-lived plugin session |
| OAuth interception / CSRF | PKCE + `state`; one-time codes; exact redirect matching; no WebView |
| Secret URL leakage (ICS) | Treat as bearer secret; redact logs; encrypt at rest; rotation |
| SSRF via feed URL | Host/IP allowlist checks, redirect re-validation, no non-HTTP schemes |
| Malicious calendar content | Treat summaries/descriptions as untrusted data; never execute instructions from them; sanitize HTML |
| Shared device | App-level lock; short session lifetime; revoke on sign-out |

### 7.2 Principles

- **Provider credentials are server-side only.** The Android app never sees a
  refresh token and never calls a provider directly.
- **One credential per account**, stored in a vault, referenced by `auth_ref`.
  Deleting the account deletes the credential.
- **Least privilege.** Request only the scopes the connector actually uses.
  Prefer read-only scopes, but note that Google cannot incrementally authorize
  installed apps, so its read and write scopes must be requested together.
- **Revocable.** "Disconnect" revokes the token at the provider where the
  provider supports it, then deletes the local credential.
- **Observable.** Record auth events (connect, refresh, revoke, failure) without
  recording the secret.

### 7.3 Per-provider flows

| Provider | Flow | Token storage | Notes |
|---|---|---|---|
| CalDAV (iCloud/Nextcloud) | HTTP Basic with app password over TLS | Encrypted username + password | Validate with a `PROPFIND` before saving; support a per-account custom CA; refuse plain HTTP |
| CalDAV (Fastmail) | App password (MVP), OAuth later | Encrypted | Fastmail supports OAuth for native public clients without manual registration |
| Google | App `AuthorizationClient.authorize()` with `requestOfflineAccess(serverClientId)` → `serverAuthCode` → gateway exchange | Gateway holds refresh token + access token cache | Official Android flow; no custom scheme or loopback; handle granular consent |
| Microsoft | Gateway web OAuth with PKCE, or device code | Gateway holds refresh token | `common` tenant; admin consent for org accounts; renew subscriptions separately |
| ICS | None, or Basic/custom header | URL/credentials encrypted | Treat URL as a secret; never echo it in full |

**Google details.** The app uses the Android OAuth client (package + SHA-1) and
asks for offline access against the Web-application `serverClientId`. The
gateway exchanges the one-time code with the Web client ID/secret and stores
the refresh token. Access tokens are cached in memory and refreshed
proactively. `invalid_grant` means re-consent; never retry it in a loop.
Because sensitive-scope apps in Testing mode get 7-day refresh tokens, document
that the OAuth consent screen must be **In production** for reliable sync, even
if the app is unverified and shows a warning.

**Microsoft details.** Prefer gateway-brokered web OAuth when the gateway has a
browser-reachable HTTPS origin (Tailscale with a trusted certificate is ideal).
Otherwise use device code flow: the gateway displays
`https://microsoft.com/devicelogin` and a code in the app; it polls the token
endpoint until the user approves. Both flows yield a refresh token for
`Calendars.ReadWrite offline_access`. Do not use the implicit flow.

**Fastmail OAuth (post-MVP).** Fastmail supports OAuth for native clients
using a private-use redirect URI, and does not require manual client
registration for the open-public-client profile. If added, use the same
app-brokered pattern as Google: the app gets a code, the gateway exchanges it.

**CalDAV OAuth (post-MVP).** Only useful for providers that support it. Keep
the generic connector on Basic auth; add an auth strategy interface rather
than branching inside the WebDAV client.

### 7.4 Secret storage

- Use envelope encryption: a master key from the OS keyring, TPM, or a
  deployment-provided secret; a per-account data key wrapped by it.
- Never store secrets in the same SQLite file as the event cache without
  encryption. Prefer a separate vault table or file.
- Redact `Authorization`, `Set-Cookie`, feed URLs, and OAuth codes from logs and
  diagnostics. The Hermes app already has a redacted diagnostics log; extend
  the same rule to the plugin.
- Exclude vault files from backups unless the user opts in, and document that
  a restored backup without the master key cannot sync.
- Rotate the plugin's app session tokens on a schedule and on sign-out.

### 7.5 Consent and scopes

| Provider | Scope | Why |
|---|---|---|
| Google | `https://www.googleapis.com/auth/calendar.readonly` | List calendars and read events |
| Google | `https://www.googleapis.com/auth/calendar.events` | Create/update/delete events |
| Microsoft | `Calendars.ReadWrite` | Read/write events |
| Microsoft | `offline_access` | Refresh token |
| Microsoft | `User.Read` | Optional: account identity for display |
| Fastmail | CalDAV scope (if OAuth) | Read/write calendars |

Read-only is enforced by the plugin's `writable` flag, not by omitting scopes.
Google does not support incremental authorization for installed apps, so
request both scopes up front and degrade gracefully if the user grants only
one.

---

## 8. MVP order and two-way strategy

### 8.1 Recommendation

**Build in this order:**

| Phase | Deliverable | Why here | Exit criteria | Effort |
|---|---|---|---|---|
| 0 | Canonical store, iCalendar codec, recurrence/timezone engine, fake provider, tool API skeleton | Everything else depends on the model; catch timezone/recurrence bugs before any network code | Round-trip RFC 5545 fixtures; expansion passes DST tests; fake provider passes pull/push tests | 1.5–2 wk |
| 1 | **ICS import** (read-only feeds) | Zero auth, zero write risk; proves parser, diffing, and UI; immediately useful (holidays, sports, shared feeds) | Import Google/Outlook/Apple published feeds; refresh honors ETag/interval; malformed feed does not break others | 1–1.5 wk |
| 2 | **CalDAV two-way** (iCloud, Fastmail, Nextcloud) | One connector covers three providers; standard ETags and sync-token; no app review; exercises the full conflict engine | Create/update/delete on all three; 412 recovery; full resync; recurrence exceptions; conflict inbox | 3–4 wk |
| 3 | ICS export/publish | Cheap, useful, no write path; lets users subscribe to agent-created plans from any calendar app | Stable UID export; token rotation; subscribers update rather than duplicate | 0.5–1 wk |
| 4 | **Google Calendar** two-way | High user value but high auth/verification friction; engine already proven | OAuth connect via Android; syncToken + 410 recovery; recurring instances; quota-safe polling | 2–3 wk |
| 5 | **Microsoft Graph** two-way | Work accounts; most auth and subscription complexity | Delta window + deltaLink recovery; subscription renewal; personal and work accounts | 3–4 wk |

**MVP = Phases 0–2** (plus Phase 3 if there is room): read-only feeds and
two-way CalDAV. That is roughly **6–8 engineer-weeks** with tests, and it
covers Apple, Fastmail, Nextcloud, and every ICS publisher without a single
OAuth review.

### 8.2 Why CalDAV before Google

- **Coverage per unit of work.** One connector reaches iCloud (the default for
  iPhone users), Fastmail, Nextcloud, and self-hosted servers.
- **No app review.** Google's sensitive scopes require verification for public
  distribution; CalDAV needs an app password.
- **It forces the hard parts early.** WebDAV ETags, sync tokens, recurrence
  exceptions, and 412 recovery are the same problems Google and Graph have.
  Solving them on a protocol with a readable spec is easier than learning them
  through OAuth and quota errors.
- **Self-hosted alignment.** Hermes is a private-network, self-hosted product.
  Its audience skews toward people who run their own services and are more
  likely to have Nextcloud or Fastmail than a Google Workspace domain, though
  this should be checked against actual users (see §8.2's swap rule).

**When to swap.** If user research shows >50% of target users are Google-only,
move Google to Phase 2 and CalDAV to Phase 3. The engine, store, conflict
rules, and tool API are identical; only the adapter and auth broker move. Do
not build both in parallel: two half-finished connectors are worse than one
finished one.

### 8.3 Two-way vs one-way

| Substrate | Direction | Recommendation |
|---|---|---|
| ICS import | One-way (pull) | Always read-only; no writeback, ever |
| ICS export | One-way (push) | Read-only published feed; no inbound writes |
| CalDAV | Two-way | Yes, but writes opt-in per account/collection and limited to the §4.5 field set |
| Google | Two-way | Same engine and limits |
| Graph | Two-way | Same engine and limits |
| Provider ⇄ provider | None | No mirroring in the MVP; revisit only with loop budgets and explicit user intent |

Read-only is a first-class mode, not a failure. A user may want the agent to
see their calendar but never change it. Every account starts read-only; a
separate "allow changes" toggle enables the outbox. This also gives a safe
rollout path: ship Phase 2 read-only, then enable writes behind a flag.

### 8.4 What is deliberately deferred

- Attendee invites and RSVP (first post-MVP).
- Free/busy federation and `getSchedule`.
- Google/Graph webhooks in the default deployment (polling is the baseline).
- Fastmail/CalDAV OAuth.
- Cross-provider mirroring.
- Shared-calendar ACL management.
- `VTODO` and reminders-as-tasks.
- Attachments and conference data.
- Multi-user tenancy.

### 8.5 Success metrics

| Metric | Target |
|---|---|
| Initial sync (1 year, 2k events) | < 60 s |
| Incremental pull p95 | < 5 s for < 100 changes |
| Push success on first attempt | > 98% excluding 412 retries |
| 412 recovery rate | > 99% within 3 attempts |
| Duplicate events after retries | 0 |
| Conflict rate (auto-resolved + manual) | < 0.5% of changed events |
| Auth failures surfaced within one cycle | 100% |
| Outbox age p95 | < 2 minutes when online |

---

## 9. Agent tool surface and safety

The sync engine is invisible to the agent; it sees calendar tools. Keep the
surface small and explicit.

| Tool | Input | Notes |
|---|---|---|
| `list_calendars` | — | Names, colors, writable flag, account |
| `list_events` | `from`, `to`, `calendar_ids`, `query`, `limit` | Local store; ISO 8601 with offset or `Z` |
| `get_event` | `event_id` | Includes recurrence scope and provider link |
| `search_events` | `query`, `from`, `to` | Local full-text; no provider round trip |
| `find_free_slots` | `from`, `to`, `duration`, `calendars`, `working_hours` | Union of busy blocks; no attendee availability in the MVP |
| `create_event` | fields, `calendar_id`, `idempotency_key` | Draft by default; requires `allow_changes` |
| `update_event` | `event_id`, patch, `scope` (`this`/`this_and_future`/`all`) | Scope required for recurring events |
| `delete_event` | `event_id`, `scope` | Always requires confirmation |
| `add_feed` / `remove_feed` | URL / feed id | Read-only |
| `sync_now` | `account_id` or all | Rate-limited; returns status |

Safety rules:

1. **Draft by default.** A write is staged in the outbox as a draft and
   requires a user confirmation unless the account has auto-commit enabled and
   the event has no attendees.
2. **No attendee changes** in the MVP. If the agent is asked to invite someone,
   it explains that invitations are not enabled and can create the event
   without guests.
3. **Destructive actions require confirmation.** Delete, series edit, and time
   changes on events with guests are never silent.
4. **Prompt injection is untrusted input.** Event titles, descriptions, and
   attendee names can contain instructions. The tool layer must delimit them as
   data, never as system instructions, and must not let calendar content cause
   a tool call chain.
5. **Rate-limit the agent.** A runaway loop must not create hundreds of events.
   Cap writes per minute and per hour per account.
6. **Audit everything.** Log actor (agent/user), tool, event, before/after
   hash, and outcome. Do not log full descriptions.
7. **Respect privacy.** Only send event content to the LLM when the user's
   request needs it; prefer summaries and IDs.

---

## 10. Testing and operations

### 10.1 Test strategy

- **Codec tests:** RFC 5545 fixtures (folding, escaping, `VTIMEZONE`,
  `DURATION`, `RECURRENCE-ID`, `EXDATE`, all-day, floating time), round-trip
  bijectively where the source is valid.
- **Recurrence tests:** DST transitions (US/EU/southern), leap days, monthly
  `BYDAY`, `COUNT`/`UNTIL`, infinite series caps, orphaned exceptions.
- **Merge property tests:** determinism, idempotence (merge twice = once),
  commutativity where defined, no field loss, deletion dominance.
- **Fake provider:** an in-process CalDAV/Google/Graph simulator with ETags,
  sync tokens, 412/410/429 injection, clock skew, duplicate UID, and
  delete-then-edit races. Run the full engine against it in CI.
- **Recorded HTTP:** sanitized cassettes for real provider responses; no
  tokens, no personal data.
- **Live smoke tests:** one account per provider in a test tenant, nightly,
  read-only unless a dedicated test calendar is used.
- **Failure tests:** kill the process mid-push, expire a token mid-sync, return
  a truncated ICS, move the clock, exceed quota.

### 10.2 Observability

Per account and collection, expose:

- last successful pull/push, cursor age, outbox depth, oldest pending op;
- counts by error class (`auth`, `rate_limit`, `conflict`, `parse`, `network`);
- conflict rate and auto-resolution rate;
- initial sync duration and event count;
- token refresh failures and reauth prompts.

Trace one sync cycle with a correlation ID. Redact secrets and event bodies.
Alert on "auth failed", "cursor invalid repeatedly", and "outbox older than 1
hour".

### 10.3 Recovery

- **Rebuild from provider:** drop the cursor and cache for one collection, keep
  dirty local events, full sync, re-merge. Safe at any time.
- **Restore a revision:** reapply a snapshot through the normal outbox path, so
  the provider sees a normal edit.
- **Resolve a stuck outbox:** inspect the last error, refresh the ETag, retry,
  or discard with a revision backup.
- **Disconnect:** revoke, delete credentials, keep or purge the local cache per
  user choice.

---

## 11. Open questions

1. **Host model.** Is the plugin a gateway-side module with its own process, or
   an in-app service? This changes token storage and background scheduling; the
   adapter design does not change.
2. **Local-only calendar.** Should the plugin have a built-in calendar with no
   external account? It is trivial to add with the same store and is useful for
   agent-created plans, but it needs a decision on export/backup.
3. **Google distribution.** Is this personal-only (unverified, production
   status, warning screen) or distributed (verification, privacy policy,
   possible security assessment)? This is a weeks-scale decision, not an
   implementation detail.
4. **Attendee invites.** Post-MVP, but do we ever let the agent add guests? If
   yes, the confirmation UI and audit requirements need to be designed before
   the adapter writes attendee fields.
5. **Multi-account defaults.** Which account is the agent's default for
   writes? Never guess; require an explicit default per profile.
6. **Retention.** 30-day revisions and 90-day tombstones are proposals; confirm
   against the user's backup/privacy expectations.
7. **Shared calendars.** Read-only shared collections are assumed; confirm
   whether Nextcloud/iCloud delegated write access is in scope.

---

## 12. References

- RFC 4791 — CalDAV: <https://www.rfc-editor.org/rfc/rfc4791>
- RFC 4918 — WebDAV: <https://www.rfc-editor.org/rfc/rfc4918>
- RFC 5545 — iCalendar: <https://www.rfc-editor.org/rfc/rfc5545>
- RFC 6578 — Collection Synchronization for WebDAV: <https://www.rfc-editor.org/rfc/rfc6578>
- RFC 6638 — CalDAV Scheduling: <https://www.rfc-editor.org/rfc/rfc6638>
- RFC 7986 — New iCalendar Properties: <https://www.rfc-editor.org/rfc/rfc7986>
- RFC 8252 — OAuth 2.0 for Native Apps: <https://www.rfc-editor.org/rfc/rfc8252>
- Google Calendar API — sync: <https://developers.google.com/workspace/calendar/api/guides/sync>
- Google Calendar API — push: <https://developers.google.com/workspace/calendar/api/guides/push>
- Google Calendar API — quota: <https://developers.google.com/workspace/calendar/api/guides/quota>
- Google Workspace agent-tool quotas (May 2026): <https://developers.google.com/workspace/tools-safety>
- Google Identity — Android authorization: <https://developer.android.com/identity/authorization>
- Google OAuth 2.0 — native apps: <https://developers.google.com/identity/protocols/oauth2/native-app>
- Google CalDAV guide: <https://developers.google.com/workspace/calendar/caldav/v2/guide>
- Microsoft Graph — delta query: <https://learn.microsoft.com/graph/delta-query-overview>
- Microsoft Graph — event delta: <https://learn.microsoft.com/graph/api/event-delta>
- Microsoft Graph — change notifications: <https://learn.microsoft.com/graph/change-notifications-overview>
- Apple — app-specific passwords: <https://support.apple.com/en-us/102654>
- Fastmail — app passwords: <https://www.fastmail.help/hc/en-us/articles/360058752854>
- Fastmail — OAuth for developers: <https://www.fastmail.com/dev/>
- Nextcloud — CalDAV with app passwords: <https://help.nextcloud.com/t/caldav-and-oauth2-oidc/214815>
