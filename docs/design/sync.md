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
| Auth | Provider tokens live **only in the plugin backend**, encrypted at rest; the plugin's local web UI brokers consent when needed and never holds provider tokens |
| Conflicts | Field-level three-way merge; **remote wins on scheduling fields**, guarded last-writer-wins on text, **delete wins**, losing versions retained for 30 days |
| Overall model | **Local-first cache + outbox; each remote collection is authoritative for its own events** |
| Calendar topology | **One profile-private calendar set + one shared global calendar**; every sync is an explicit mapping with a direction (`pull`, `push`, `two_way`) |

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
- the profile/global calendar topology, storage layout, and mapping rules;
- the sync engine (pull, push, cursors, retries, full resync);
- conflict detection and resolution rules;
- authentication and secret storage;
- the order in which to build the connectors, and what "done" means for each.

It does **not** cover UI layout, visual design, agent prompt design, or the
Hermes gateway plugin runtime. Those belong to the sibling design docs.

### 1.2 Assumptions

These assumptions are load-bearing; if one is wrong, the affected section
changes but the protocol comparison and MVP order still hold.

1. **The plugin is a Python backend module** that can make outbound HTTPS
   requests and persist state. It runs inside the Hermes gateway process or
   under it as a plugin host process. The plugin's local web UI and any future
   native clients are thin consumers of the plugin's local HTTP/WebSocket API.
2. **One user per gateway profile**, with a small number of calendar accounts
   (1–5). This is a self-hosted personal assistant, not a multi-tenant SaaS.
3. The plugin runs on a host that may be **headless** (a home server, a VPS, a
   container). Its local web UI is reached with a browser on the same machine
   or another machine on the private network; the gateway is **not** assumed
   to have a public HTTPS hostname.
4. A system browser is available on whichever machine the user is sitting at,
   and the plugin can listen on a loopback port. When the browser and plugin
   are on different machines, the user can SSH-forward that port or paste the
   redirect URL back into the UI (see §2.2).
5. The local web UI is the only first-party client for now; future native
   clients consume the plugin's local API. Third-party CalDAV/ICS clients may
   also write to the same external calendars.
6. The plugin may be offline or asleep; sync must be resumable and idempotent.
7. Event contents are private. They must not be logged, and must only be sent
   to an LLM when the user's request requires it.

### 1.3 Non-goals for the MVP

- iTIP/iMIP scheduling: sending invitations, processing replies, RSVP.
- Free/busy federation across providers (`freebusy` queries).
- Room/resource booking, delegation, and calendar ACL management.
- `VTODO`, `VJOURNAL`, attachments, and conference-data creation.
- Direct provider-to-provider mirroring (all sync goes through a local
  calendar and an explicit mapping; cross-provider copies are fan-out, not a
  direct peer-to-peer merge).
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
│ Local web UI     │◀───────▶│  └─────┬─────┘        └────────┬─────────┘  │
│ (browser)        │ plugin  │        │                       │            │
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
- **Each event belongs to exactly one local calendar; mappings may publish it
  to several remotes.** There is no direct provider-to-provider merge in the
  MVP, and at most one mapping per local calendar is two-way (§5.6). That
  avoids the classic loop and duplicate problems of calendar mirroring.
- **Remote is authoritative for conflict resolution**, because another client
  may have changed the event and because scheduling fields (time, attendees)
  must not diverge between devices.

Why not proxy reads straight through to providers? Because the agent needs
sub-100 ms tool calls, cross-account free/busy, and a stable view when the
network is down. It also needs a place to detect conflicts; a pure proxy
cannot tell "remote changed" from "local changed".

### 2.2 Deployment variants

The engine and adapters are identical in every topology. Only the OAuth
mechanics and where the browser runs change.

| Topology | Where sync runs | OAuth mechanics |
|---|---|---|
| Same host | Plugin process on the host; browser on the same machine | Loopback redirect (`http://127.0.0.1:<port>`) works directly |
| Headless host, browser elsewhere | Plugin process on the server | SSH port-forward the callback port, or paste the failed redirect URL back into the web UI |
| Future native client | Plugin backend | Client calls the plugin's local API; provider OAuth stays in the plugin |

The plugin owns all provider credentials and all long-running sync work. A
native client, if one is ever built, is only another consumer of the local API
and must never hold a provider refresh token. A headless deployment is the
normal case, not an exception: design the OAuth flow so it works without a
browser on the plugin host.

For Google, the supported flow is an OAuth 2.0 **Desktop app** client with a
loopback redirect and PKCE. On a headless host, prefer SSH port forwarding:

```sh
ssh -L 8765:127.0.0.1:8765 user@calendar-host
```

Then open the authorization URL in the local browser; the redirect to
`127.0.0.1:8765` is tunneled to the plugin's listener. If forwarding is not
possible, the fallback is to complete consent, let the browser fail to connect
to the loopback address, and paste the full redirect URL (which contains a
one-time authorization code) into the plugin's web UI. That paste is a
workaround, not Google's deprecated out-of-band flow: the plugin must validate
`state` and PKCE, use the code once, and treat the URL as a secret. Microsoft
device-code still works on a headless host; Google device-code does not permit
Calendar scopes (§3.3).

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

**Auth.** OAuth 2.0 with a Google Cloud **Desktop app** client. This is a public
client, so the flow uses **PKCE** (S256) and a loopback redirect; the client
secret is not treated as confidential. The plugin:

1. generates a PKCE `code_verifier`/`code_challenge` and a random `state`;
2. starts a one-shot HTTP listener on `http://127.0.0.1:<port>` (loopback only,
   ephemeral port, short timeout);
3. opens the system browser at
   `https://accounts.google.com/o/oauth2/v2/auth` with
   `response_type=code`, the scopes below, `redirect_uri`, `code_challenge`,
   and `state`;
4. validates `state`, then exchanges the code plus `code_verifier` at
   `https://oauth2.googleapis.com/token`;
5. encrypts and stores the refresh token in the plugin's credential vault.

Google documents this Desktop app flow, including PKCE and the loopback
redirect, at
<https://developers.google.com/identity/protocols/oauth2/native-app>.

**Headless deployment.** If the browser is on a different machine than the
plugin, forward the callback port (`ssh -L 8765:127.0.0.1:8765 user@host`) and
open the authorization URL locally. If forwarding is impossible, complete
consent and copy the full `http://127.0.0.1:<port>/?code=...&state=...` URL from
the browser's error page back into the plugin's web UI; the plugin validates
`state`, checks PKCE, and exchanges the code once. This paste is a pragmatic
workaround, **not** Google's out-of-band flow, which is deprecated and
unsupported
(<https://developers.google.com/identity/protocols/oauth2/resources/oob-migration>).
Treat the pasted URL and code as secrets.

**Google device-code is not an option.** Google's device authorization flow is
restricted to a small allowlist of scopes — OIDC `email`/`openid`/`profile`,
Drive `drive.appdata`/`drive.file`, and YouTube — which does **not** include
Calendar
(<https://developers.google.com/identity/protocols/oauth2/limited-input-device#allowedscopes>).
Use the Desktop app flow above. Microsoft device-code remains a valid fallback
for Graph (§3.4).

Scopes: `calendar.readonly` (list calendars and read) plus `calendar.events`
(read/write events). Incremental authorization is not supported for installed
apps, so request both at once and handle the case where the user grants only
one. Google calls both scopes *sensitive*: public distribution requires
verification, and an app in **Testing** publishing status gets refresh tokens
that expire after **7 days** (and a 100-test-user cap). A self-hosted personal
deployment can run unverified but should be set to **In production** with the
unverified-app warning, otherwise the 7-day expiry breaks 24/7 sync. Unverified
production apps are still capped at 100 users, which is fine for personal use
and a blocker for distribution. See
<https://developers.google.com/identity/protocols/oauth2#expiration> for the
refresh-token expiry rules and
<https://support.google.com/cloud/answer/9110914> for verification.

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

1. **Plugin-brokered web OAuth** with PKCE and `state`, callback on the
   gateway's HTTPS origin. Cleanest when the gateway is browser-reachable
   (Tailscale + a trusted certificate).
2. **Device code flow**: the gateway requests a code, shows the URL and code
   in the plugin's web UI, the user approves on their phone or laptop, and the
   gateway polls for the refresh token. This is the reliable fallback when the
   gateway has no browser-reachable HTTPS origin.

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

- **Canonical identity is `(collection, local_uid)`** for a single event or a
  recurrence master, extended to `(collection, local_uid, recurrence_id)` for
  an exception. For CalDAV/ICS the local UID is the iCalendar `UID`; for
  Google/Graph it is a generated UUID, with the provider ID stored as a
  separate unique attribute.
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

## 5. Calendar topology and sync mapping

### 5.1 Requirements

The product has three objects that must not be conflated:

| Requirement | Meaning |
|---|---|
| **R1 — profile calendar** | Each Hermes profile owns its own local calendar(s). Its events, mappings, credentials, and sync state are private to that profile. |
| **R2 — global calendar** | Exactly one global local calendar exists on a gateway. Every profile can read and write it. |
| **R3 — explicit mapping** | The user maps a local calendar to an external account + calendar, and chooses the direction for that mapping: `pull`, `push`, or `two_way`. Nothing syncs without a mapping. |

Two supporting rules keep this predictable:

- **R4 — isolation.** Profile-private data stays in profile-scoped storage. Only
  the global calendar and its non-secret sync metadata live in the shared store.
- **R5 — one syncer.** An external calendar is synced by exactly one profile.
  Other profiles may write the global calendar, but they do not run its sync.

### 5.2 Local calendars, external calendars, and mappings

- A **local calendar** is a container of canonical events. It has no inherent
  remote counterpart. It is either `profile`-scoped or the single `global`
  calendar.
- An **external account** is a provider login (Google account, CalDAV server,
  Microsoft account, ICS feed). It is profile-scoped because its credentials
  are profile-scoped.
- An **external calendar** is a calendar or feed discovered under an account
  (Google `primary`, an iCloud collection, an Outlook calendar, an ICS URL).
- A **mapping** is an edge from one local calendar to one external calendar,
  with a direction and a conflict policy. The same local event can therefore
  have several remote counterparts, and one local calendar can aggregate
  several remotes.

This is deliberately not "sync the calendar". A calendar is a container;
mappings are the only thing the sync engine acts on. That is what makes the
global calendar, aggregation, and publishing composable.

### 5.3 Where the data lives

| Store | Location | Contents | Scope |
|---|---|---|---|
| Profile DB | `plugin_db()` as provided by Hermes | Profile local calendars and events, external accounts (`auth_ref` only), external-calendar cache, mappings owned by this profile, `mapping_event`, outbox, cursors, conflicts, revisions | One profile |
| Global DB | `calendar.global_db_path`, default `$HERMES_HOME/plugin-data/hermes-calendar/global/calendar.db` | The one global local calendar, its events, its mappings, `mapping_event`, outbox, `change_log`, `sync_lease`, schema metadata | Shared by every profile on the gateway |
| Credential vault | Beside the profile DB, encrypted | Passwords, refresh tokens, feed URLs | One profile |

The global DB is an **explicit, documented exception** to the profile-scoped
`plugin_data_dir()` / `plugin_db()` rule (see the Hermes plugin API research
notes, `docs/research/hermes-plugin-api.md` on `feature/research-hermes-api`,
section 12 of that document). The current plugin API has no official
cross-profile storage primitive, so the shared path is a deployment
configuration, not a hidden use of `plugin_db()`. The global DB contains only
the global calendar
and non-secret sync state: no credentials, no profile-private events, no feed
URLs. Everything else stays in the profile's own database.

The path is configurable because deployments differ. The plugin must:

- create it with `0700` directories and `0600` files. This assumes every
  profile runs as the same OS user (or a shared group); if Hermes runs profiles
  under different OS users, use the hub-service fallback or a carefully
  group-owned path instead of weakening the file mode;
- refuse a path inside the plugin install directory (which `update`/`remove`
  replaces);
- prove at startup that the path is writable and shared, and fail closed with a
  clear diagnostic if it is not. Each profile writes a sentinel row keyed by its
  profile id; if two profiles cannot see each other's sentinel within a short
  window, the path is not actually shared and the plugin must not claim the
  global calendar is available;
- remove it on uninstall only with explicit confirmation.

**Why shared SQLite rather than a service.** Hermes can run each profile in a
separate plugin-host process, so in-memory sharing is not available. SQLite in
WAL mode is the smallest cross-process mechanism that supports concurrent
readers and a single writer, needs no daemon, and survives restarts. It
requires a **local filesystem**: WAL locking is not reliable on NFS, SMB, or
other network mounts, so a shared network path must use the hub-service
fallback instead. The alternative — a designated **hub profile** that owns the
global calendar and serves it over a local Unix-socket/HTTP API — is cleaner
isolation but adds a service, authentication, and a second failure domain; keep
it as a fallback if an operator cannot grant a shared local path. If Hermes
later exposes a gateway-wide storage API, move the global DB behind it without
changing the mapping model.

The profile DB and global DB are **not joined in a single transaction**.
SQLite does not guarantee atomic multi-database commits in WAL mode. Every
data-plane operation is therefore single-database, and control-plane operations
that touch both (creating a global mapping, adopting a mapping) are idempotent
and reconciled. This is why a mapping row lives in the same database as its
local calendar.

### 5.4 Schema

All timestamps are ISO 8601 UTC. Booleans are integers. IDs are UUIDs unless
noted. The event table itself is the canonical model from §4.1; the columns
shown here are the ones the mapping layer needs.

**Profile DB (`plugin_db()`):**

```sql
CREATE TABLE local_calendar (
    id                TEXT PRIMARY KEY,
    scope             TEXT NOT NULL CHECK (scope = 'profile'),
    owner_profile_id  TEXT NOT NULL,
    name              TEXT NOT NULL,
    color             TEXT,
    timezone          TEXT NOT NULL,             -- IANA, e.g. America/Chicago
    is_default        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    deleted_at        TEXT
);

CREATE UNIQUE INDEX ux_profile_default_calendar
    ON local_calendar(owner_profile_id)
    WHERE is_default = 1 AND deleted_at IS NULL;

CREATE TABLE external_account (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    provider      TEXT NOT NULL
                  CHECK (provider IN ('caldav', 'google', 'graph', 'ics')),
    display_name  TEXT NOT NULL,
    base_url      TEXT,
    auth_ref      TEXT NOT NULL,                 -- vault key, never the secret
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'reauth_required', 'disabled')),
    default_tz    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (profile_id, provider, display_name)
);

CREATE TABLE external_calendar (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL
                  REFERENCES external_account(id) ON DELETE CASCADE,
    remote_id     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    read_only     INTEGER NOT NULL DEFAULT 0,
    sync_cursor   TEXT,
    ctag          TEXT,
    last_sync_at  TEXT,
    UNIQUE (account_id, remote_id)
);

CREATE TABLE calendar_mapping (
    id                   TEXT PRIMARY KEY,
    local_calendar_id    TEXT NOT NULL,
    external_calendar_id TEXT NOT NULL
                         REFERENCES external_calendar(id),
    owner_profile_id     TEXT NOT NULL,
    direction            TEXT NOT NULL
                         CHECK (direction IN ('pull', 'push', 'two_way')),
    enabled              INTEGER NOT NULL DEFAULT 1,
    conflict_policy      TEXT NOT NULL DEFAULT 'remote_wins'
                         CHECK (conflict_policy IN ('remote_wins', 'local_wins', 'manual')),
    external_provider    TEXT NOT NULL,
    external_display_name TEXT NOT NULL,
    window_start         TEXT,
    window_end           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    deleted_at           TEXT
);

CREATE UNIQUE INDEX ux_mapping_active
    ON calendar_mapping(local_calendar_id, external_calendar_id)
    WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX ux_one_two_way_per_local
    ON calendar_mapping(local_calendar_id)
    WHERE direction = 'two_way' AND deleted_at IS NULL;

CREATE TABLE mapping_event (
    mapping_id       TEXT NOT NULL
                     REFERENCES calendar_mapping(id) ON DELETE CASCADE,
    local_event_id   TEXT NOT NULL,
    remote_event_id  TEXT NOT NULL,
    remote_etag      TEXT,
    base_hash        TEXT NOT NULL,              -- last agreed normalized content
    last_pushed_hash TEXT,                       -- echo suppression
    last_origin      TEXT NOT NULL
                     CHECK (last_origin IN ('local', 'remote')),
    remote_created_by_us INTEGER NOT NULL DEFAULT 0,
    remote_updated_at TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (mapping_id, local_event_id),
    UNIQUE (mapping_id, remote_event_id)
);

CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id      TEXT NOT NULL
                    REFERENCES calendar_mapping(id) ON DELETE CASCADE,
    local_event_id  TEXT NOT NULL,
    op              TEXT NOT NULL CHECK (op IN ('create', 'update', 'delete')),
    base_etag       TEXT,
    base_rev        INTEGER,
    payload_hash    TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'in_flight', 'done', 'parked'))
);
```

**Global DB (`calendar.global_db_path`):**

```sql
CREATE TABLE local_calendar (
    id               TEXT PRIMARY KEY,           -- seeded as 'global'
    scope            TEXT NOT NULL CHECK (scope = 'global'),
    owner_profile_id TEXT,                       -- NULL: shared
    name             TEXT NOT NULL,
    color            TEXT,
    timezone         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    deleted_at       TEXT,
    CHECK ((scope = 'global') = (owner_profile_id IS NULL))
);

CREATE UNIQUE INDEX ux_one_global_calendar
    ON local_calendar(scope)
    WHERE scope = 'global' AND deleted_at IS NULL;

CREATE TABLE event (
    id                   TEXT PRIMARY KEY,
    calendar_id          TEXT NOT NULL REFERENCES local_calendar(id),
    uid                  TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    status               TEXT NOT NULL,
    start_utc            TEXT,
    end_utc              TEXT,
    all_day              INTEGER NOT NULL DEFAULT 0,
    tzid                 TEXT,
    rrule                TEXT,
    recurrence_id        TEXT NOT NULL DEFAULT '',  -- '' for a master/single
    summary              TEXT,
    description          TEXT,
    location             TEXT,
    content_hash         TEXT NOT NULL,
    remote_updated_at    TEXT,
    rev                  INTEGER NOT NULL DEFAULT 1,
    created_by_profile_id TEXT NOT NULL,
    updated_by_profile_id TEXT NOT NULL,
    created_by_mapping_id TEXT,                  -- NULL for pre-existing events
    writeback_allowed    INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    deleted_at           TEXT,
    UNIQUE (calendar_id, uid, recurrence_id)
);

CREATE TABLE event_revision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL REFERENCES event(id),
    rev         INTEGER NOT NULL,
    snapshot    TEXT NOT NULL,                   -- JSON
    actor       TEXT NOT NULL,                   -- profile id or 'agent'
    at          TEXT NOT NULL
);

CREATE TABLE change_log (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity         TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    op             TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
    rev            INTEGER NOT NULL,
    by_profile_id  TEXT NOT NULL,
    at             TEXT NOT NULL
);

CREATE TABLE sync_lease (
    provider           TEXT NOT NULL,
    account_key        TEXT NOT NULL,            -- stable hash, no credentials
    remote_calendar_id TEXT NOT NULL,
    owner_profile_id   TEXT NOT NULL,
    lease_expires_at   TEXT NOT NULL,
    heartbeat_at       TEXT NOT NULL,
    PRIMARY KEY (provider, account_key, remote_calendar_id)
);
```

`account_key` is a non-secret, stable identifier for the provider account:
Google's `sub` (or a salted hash of the email), Microsoft's user id, a salted
hash of `base_url + username` for CalDAV, or a salted hash of the feed URL for
ICS. Never put a token, password, or feed URL in `account_key`.

The global DB also carries `calendar_mapping`, `mapping_event`, and `outbox`
with the same shape as the profile DB, plus a `schema_meta` table. A mapping
row lives with its local calendar: profile mappings in `plugin_db()`, global
mappings in the shared DB. In the global copy, drop the SQL foreign key on
`external_calendar_id` — the external calendar lives in a profile DB — and
denormalize `external_provider` and `external_display_name` for display.
`mapping_event.local_event_id` and `external_calendar_id` are
application-level references, not SQL foreign keys, because they may point at
the other database. `mapping_event.remote_created_by_us` records whether this
plugin created the remote event, which is what makes the explicit
"delete remote copies" option safe; `event.created_by_mapping_id` and
`writeback_allowed` keep pull-imported events from being published by another
mapping.

### 5.5 Direction semantics

Direction is a property of the mapping, not of the local calendar. One local
calendar can be read-only from one remote and writable to another.

| Direction | Pull | Push | Local edits | Remote edits | Typical use |
|---|---|---|---|---|---|
| `pull` | Yes, incremental | No | Rejected for imported events; local notes only | Applied | Aggregation, read-only subscriptions, shared calendars |
| `push` | No | Yes, incremental | Pushed | Ignored | Publish the agent's plan to a calendar the user reads elsewhere |
| `two_way` | Yes | Yes | Pushed | Applied and merged | The normal personal calendar mapping |

- `pull` mappings never create outbox rows. Imported events are read-only and
  are stored with `writeback_allowed = 0`, so no other mapping will push them
  by accident. To edit one or publish it, the user **detaches** it into a
  profile calendar (a copy with a new UID and `writeback_allowed = 1`), which
  breaks the link and prevents accidental writeback.
- `push` mappings do not import existing remote events. Creation offers an
  optional "publish existing local events" action; by default, only changes
  after the mapping is created are published.
- `two_way` mappings run the full engine and conflict rules. A local calendar
  has at most one active `two_way` mapping (§5.6).

### 5.6 One-to-many and many-to-one rules

**One local calendar to several remotes** is allowed. Each mapping keeps its
own `mapping_event` rows, its own cursor, and its own outbox. A local edit fans
out to every `push`/`two_way` mapping:

```
local "Kevin" ── two_way ──▶ Google primary
              ── push    ──▶ Fastmail "Work"      (publish only)
              ── pull    ◀── ICS "US holidays"    (read-only)
```

Rules:

- **One primary writer.** At most one active `two_way` mapping per local
  calendar. Any additional mapping must be `pull` or `push`. Multiple
  authoritative remotes would make "who wins" ambiguous and create feedback
  loops; the UI explains this and defaults the second mapping to `push`.
- **One local home per remote.** An external calendar may be actively mapped to
  at most one local calendar across the whole gateway. This is enforced by the
  `sync_lease` and checked at mapping creation. It prevents the same remote
  event being imported twice. If a user wants the same events visible in two
  local calendars, one calendar should be the home and the other should read
  it through the product's calendar-list UI, not through a second mapping.
- **Aggregation is pull-only for the extra sources.** Several remotes can feed
  one local calendar (many-to-one), but only one of them may be `two_way`; the
  rest are `pull`. Imported events keep `writeback_allowed = 0`, so the local
  calendar's two-way mapping does not accidentally publish another remote's
  events. Events from different mappings remain distinct even if their `UID`s
  collide (§5.7).
- **A mapping never crosses profiles.** A global-calendar mapping is owned by
  one profile; other profiles write events to the global calendar, but the
  owner is the only one that syncs it. If the owner disappears, the mapping
  pauses until another profile re-authenticates the account and adopts it.
- **Read-only remotes stay read-only.** Shared calendars and ICS feeds can only
  be `pull`. The UI and the tool layer both reject a push to a read-only
  external calendar.

### 5.7 Identity and deduplication across mappings

`mapping_event` is the only link between a local event and a remote event:

- `(mapping_id, local_event_id)` is unique: one local event has at most one
  counterpart per mapping.
- `(mapping_id, remote_event_id)` is unique: one remote event belongs to one
  mapping and one local event.
- A local event with three mappings has three `mapping_event` rows and three
  remote IDs. That is the fan-out case.
- **Do not dedupe across mappings by `UID`.** Different providers copy,
  rewrite, and collide UIDs (holiday feeds are the classic case). A new remote
  event is matched only through its own mapping's link. Cross-mapping dedupe is
  an explicit user action ("link these events") and is not in the MVP.
- A duplicate `(UID, RECURRENCE-ID)` inside one mapping is a provider error:
  keep the oldest, flag the other, and do not create a second local event.

### 5.8 Loop prevention with overlapping mappings

The single-`two_way` rule removes the direct A↔B feedback path. The remaining
echo path is a fan-out write coming back through the same mapping:

1. A local edit stages outbox rows for every `push`/`two_way` mapping, but only
   for events with `writeback_allowed = 1`.
2. Each push records the normalized content hash in `mapping_event.last_pushed_hash`.
3. A later pull from that mapping compares the remote content hash with
   `last_pushed_hash`. If equal, it is our own write; skip it. If different,
   it is a genuine remote edit and goes through the normal merge.
4. The hash excludes volatile fields (`DTSTAMP`, `LAST-MODIFIED`, `SEQUENCE`,
   provider ETags), so provider normalization does not defeat the check.
5. `last_origin` is diagnostic only. The hash is the correctness mechanism;
   an `origin=local` flag alone is insufficient because two mappings can both
   be local-origin.
6. A `pull` mapping never writes, and a `push` mapping never reads remote
   events, so neither can participate in a loop.
7. If the user maps two remotes that contain copies of the same event, the
   plugin **does not merge them**. It keeps two local events and warns about the
   duplicate UID. Silent merging is how calendars lose data.

Mapping creation also rejects overlap it can see: the same external calendar
already mapped, the same `(provider, account_key, remote_calendar_id)` held by
another profile's lease, or a `push`/`two_way` mapping to a read-only remote.
Lease acquisition and mapping insertion are serialized in the global DB; a
profile that loses the race gets a `409` naming the current owner (profile
display name only, never credentials).

Worked example: local calendar `L` has a `two_way` mapping to Google `A` and a
`push` mapping to Fastmail `B`. The user edits an event. Outbox rows are staged
for both. Push to `A` writes it and records `last_pushed_hash(A)`; push to `B`
does the same. A later pull from `A` sees our exact content, matches
`last_pushed_hash(A)`, and skips it. `B` never pulls. If the user also adds a
`pull` mapping to an ICS feed, a change there enters `L` once and is pushed to
`A` and `B`; the returning echoes are suppressed per mapping. No mapping can
write back into another mapping's remote directly, so there is no cycle.

### 5.9 Concurrent writes to the global calendar

The global DB is opened by several profile processes, so concurrency is
explicit:

- **WAL + busy timeout.** `journal_mode=WAL`, `synchronous=NORMAL`,
  `busy_timeout=5000`, `foreign_keys=ON`. Readers do not block the writer.
- **Short write transactions.** Use `BEGIN IMMEDIATE` for any transaction that
  writes, to avoid lock-upgrade deadlocks. Retry `SQLITE_BUSY`/`SQLITE_LOCKED`
  with exponential backoff and jitter; surface a clear "calendar busy" error
  after the retry budget.
- **Optimistic row version.** Every event has `rev`. An update is:

  ```sql
  UPDATE event
     SET summary = ?, description = ?, start_utc = ?, end_utc = ?,
         content_hash = ?, rev = rev + 1,
         updated_by_profile_id = ?, updated_at = ?
   WHERE id = ? AND rev = ? AND deleted_at IS NULL;
  ```

  Zero rows changed means another profile won; return `409` with the current
  row so the caller can reload or merge. Do not blind-retry an overwrite. Both
  versions are retained in `event_revision`; a same-field overlap is surfaced as
  a conflict rather than silently overwritten.
- **Idempotent inserts.** `INSERT ... ON CONFLICT(calendar_id, uid, recurrence_id)
  DO NOTHING`; zero rows means the event already exists, so fetch it and treat
  the operation as an update or a duplicate, not a second event.
- **No network I/O in a transaction.** Claim outbox rows in a short
  transaction, perform the HTTP call outside it, then commit the result in
  another short transaction. A crash re-claims an in-flight row; provider-side
  idempotency keys prevent duplicates.
- **Change notification.** Append to `change_log` in the same transaction as
  the event write. The sync worker in the owning profile polls
  `WHERE seq > last_seen_seq` (a wakeup socket can be added later). Polling is
  sufficient for calendar latency. For example, profile B writes a global
  event; the transaction appends to `change_log`; profile A owns the global
  mapping, sees the change, and stages the push. B never needs the provider
  credential.
- **Single sync owner.** Before syncing an external calendar, acquire the
  lease:

  ```sql
  INSERT INTO sync_lease
      (provider, account_key, remote_calendar_id, owner_profile_id,
       lease_expires_at, heartbeat_at)
  VALUES (?, ?, ?, ?, ?, ?)
  ON CONFLICT(provider, account_key, remote_calendar_id) DO UPDATE
     SET owner_profile_id = excluded.owner_profile_id,
         lease_expires_at = excluded.lease_expires_at,
         heartbeat_at = excluded.heartbeat_at
   WHERE sync_lease.lease_expires_at < excluded.heartbeat_at
      OR sync_lease.owner_profile_id = excluded.owner_profile_id;
  ```

  Check that one row changed. Renew every 60 seconds from a dedicated
  heartbeat task, not from inside a network call, with a 180-second TTL. A
  crashed profile loses the lease automatically; take over only after the lease
  has been expired for a five-minute grace period, because a missed heartbeat
  can also mean the owner is busy or the host was suspended.
- **Migrations.** Take an exclusive `flock` on `global/migrate.lock`, read and
  bump `PRAGMA user_version`, and make every migration idempotent. Other
  profiles wait, then open the new schema.
- **Backups.** Use SQLite's backup API or `VACUUM INTO`, not a raw copy of a
  live WAL database. Back up the global DB and each profile DB separately; a
  restore that pairs mismatched profile and global schemas must run the
  reconciliation check.
- **Encryption.** The global DB contains shared calendar content. Prefer
  SQLCipher or OS full-disk encryption. If the deployment cannot share a key
  across profiles, use the hub-service fallback rather than storing plaintext.

### 5.10 Mapping lifecycle

**Create.**

1. Validate the local calendar and the external calendar (writable capability,
   not already mapped, lease obtainable).
2. Acquire the `sync_lease` for `(provider, account_key, remote_calendar_id)`.
3. Insert the mapping in the database that owns the local calendar.
4. Run the initial sync implied by the direction: `pull`/`two_way` imports the
   configured window (`-1 year` to `+2 years` by default); `push` publishes
   existing events only if the user chose that option.
5. On any failure, release the lease and leave no half-created mapping.

**Pause/resume.** Stop scheduling and release the lease on pause; keep cursors,
links, and the mapping row. Resume re-acquires the lease and runs an
incremental sync.

**Remove.** Default is **keep local copies**:

| Option | Behavior |
|---|---|
| Keep local copies (default) | Delete `mapping_event` rows and the outbox for the mapping; events stay as ordinary local events; clear `created_by_mapping_id`; pull-imported copies keep `writeback_allowed = 0` until the user promotes them; remote is untouched |
| Remove local copies | Delete only events created by this mapping and not locally modified since the last sync; archive the rest |
| Delete remote copies (explicit) | Delete only events with `mapping_event.remote_created_by_us = 1`, with confirmation; never touch pre-existing or shared events |

Always release the lease, stop the scheduler, and keep a soft-deleted mapping
row for audit and relinking. Removing an account pauses its mappings and
deletes the credential; it does not delete calendars or remote data. Removing a
profile pauses global mappings it owned; adoption requires re-authenticating
the external account in another profile.

**Relink.** If a mapping is removed and recreated for the same external
calendar, offer to relink existing local events by
`(provider, account_key, remote_event_id)` from the soft-deleted
`mapping_event` rows rather than duplicating them.

### 5.11 Google-first defaults

"Google-first" is a setup default, not a schema special case. On first run:

1. Create a profile local calendar (`My Calendar`, default, gateway timezone).
2. Connect Google with the Desktop app OAuth flow (§3.3).
3. Map Google `primary` → `My Calendar`, `two_way`, `remote_wins`, window
   `-1 year` to `+2 years`.
4. Add every other Google calendar as `pull` until the user opts into writes.
5. Create the global calendar (`Shared`) lazily, with **no mapping**. Mapping it
   is an explicit user action.

The same shape applies to other providers: the account's primary calendar gets
`two_way`, secondary/shared calendars get `pull`, ICS feeds get `pull`. If the
user tries to add a second `two_way` mapping to the same local calendar, explain
the single-primary rule and default the new mapping to `push`.

## 6. Sync engine

### 6.1 Cycle

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
    publish change notifications to the local UI
```

Pull before push. This reduces the chance of pushing a stale edit, and it
means the outbox is merged against the freshest remote state. A push failure
must never block the next pull.

### 6.2 Pull

For each changed resource:

1. Parse the remote representation into a `RemoteSnapshot`.
2. Upsert the remote snapshot into `base`.
3. If the local record is clean, fast-forward the canonical event.
4. If the local record is dirty, run the conflict merge (§7) and either
   resolve automatically or open a `Conflict`.
5. Apply deletions as tombstones, not hard deletes.

Initial full sync is paginated and resumable. Persist the page cursor so a
restart does not re-download everything. For CalDAV, repeat the sync report
until it returns no changes; for Google, page until `nextSyncToken`; for
Graph, page until `deltaLink`; for ICS, one fetch and a diff.

### 6.3 Push

Outbox operations are coalesced per event. If an event has three edits queued,
push the latest state once, not three times. Operations are ordered per event;
different events can proceed in parallel within the account's rate limit.

For each operation:

1. If the event has no remote ID, create it using the provider's idempotent
   create mechanism (§6.6).
2. Otherwise update/delete with `If-Match` on the base ETag.
3. On success, store the new ETag/version and clear the operation.
4. On `412`/`409`, refetch, re-merge from the same base, and retry (max 3,
   jittered). If it still fails, park the event in the conflict queue.
5. On `404` for an update, treat it as remote-deleted → §7.
6. On `410`/invalid cursor, full resync the collection before retrying.
7. On `429`/`503`, honor `Retry-After` and back off exponentially with jitter.
8. On `401`, refresh the token once, then mark the account `reauth_required`.

A push is only considered done when the provider accepts it. The local record
keeps a `pending` marker until the next pull confirms the accepted
representation, which also catches provider-side normalization (e.g. Google
rewriting a timezone).

### 6.4 Cursors and full resync

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

### 6.5 Scope window

Sync only a rolling window by default: **-1 year to +2 years**, configurable
per account. This bounds storage and initial sync time. The window matters for
Graph because `calendarView` delta is windowed. When the window moves:

- Widen it forward in small steps and run a delta for the new slice.
- Never assume that an event outside the window is deleted. Only tombstones
  from the provider are deletions.
- Keep events that fall out of the window until they are older than the
  retention policy, so the agent can answer "when did I last…" questions.

### 6.6 Idempotency and loop prevention

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

### 6.7 Scheduling

- Poll interval: 5 minutes for the primary calendar while the gateway is
  actively used, 15–30 minutes otherwise, ±25% jitter.
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

### 6.8 Error taxonomy

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

### 6.9 Implementation notes

- **Do not hand-roll iCalendar or `RRULE`.** Use a mature parser/expander and
  wrap it in the conformance suite from §11.1. Recurrence, DST, and
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

## 7. Conflict rules

### 7.1 Principles

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

### 7.2 Definitions

- **Base**: the last remote representation the plugin accepted and synced.
- **Local**: the current canonical record, possibly dirty.
- **Remote**: the representation just fetched.
- **Dirty field**: a field whose local value differs from base.
- **Changed field**: a field whose remote value differs from base.

### 7.3 Decision table

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

### 7.4 Field policy

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

### 7.5 Recurring events

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

### 7.6 Deletions

Deletion is the most destructive operation and the hardest to merge:

- A delete on either side wins over an edit on the other side.
- The losing content is always retained in `Revision` for 30 days.
- A remote delete followed by a local edit must **not** recreate the event
  automatically. Offer "restore as new event" in the UI; the agent may only do
  it with explicit user confirmation.
- A `404` on update, a Google `status: cancelled`, a Graph `@removed`, or a
  CalDAV href removal are all deletes.
- Tombstones persist for 90 days to absorb late echoes, then are purged.

### 7.7 Precondition failures

`412` is normal, not an error. The sequence is:

1. Refetch the event.
2. Recompute the three-way merge from the same base.
3. Retry the push with the new ETag, up to 3 times with jitter.
4. If still failing, open a `Conflict` and stop automatic retries for that
   event. The rest of the collection continues.

This must be idempotent: a retry after a timeout must first check whether the
previous write actually landed (by remote ID or marker) before creating a
duplicate.

### 7.8 Worked examples

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

### 7.9 Conflict UX

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

## 8. Authentication

### 8.1 Threat model

| Threat | Mitigation |
|---|---|
| Plugin/gateway compromise exposes calendar data | Encrypt tokens at rest with a key outside the database; least-privilege scopes; per-account revocation |
| Local web UI or native client compromise steals provider tokens | Provider tokens never leave the plugin backend; UI/clients get short-lived plugin sessions |
| OAuth interception / CSRF | PKCE + `state`; one-time codes; exact redirect matching; no embedded WebView |
| Secret URL leakage (ICS) | Treat as bearer secret; redact logs; encrypt at rest; rotation |
| SSRF via feed URL | Host/IP allowlist checks, redirect re-validation, no non-HTTP schemes |
| Malicious calendar content | Treat summaries/descriptions as untrusted data; never execute instructions from them; sanitize HTML |
| Shared browser or device | Plugin session lock; short session lifetime; revoke on sign-out |

### 8.2 Principles

- **Provider credentials are backend-only.** The local web UI (and any future
  native client) never sees a refresh token and never calls a provider
  directly; it calls the plugin's local API.
- **One credential per account**, stored in a vault, referenced by `auth_ref`.
  Deleting the account deletes the credential.
- **Least privilege.** Request only the scopes the connector actually uses.
  Prefer read-only scopes, but note that Google cannot incrementally authorize
  installed apps, so its read and write scopes must be requested together.
- **Revocable.** "Disconnect" revokes the token at the provider where the
  provider supports it, then deletes the local credential.
- **Observable.** Record auth events (connect, refresh, revoke, failure) without
  recording the secret.

### 8.3 Per-provider flows

| Provider | Flow | Token storage | Notes |
|---|---|---|---|
| CalDAV (iCloud/Nextcloud) | HTTP Basic with app password over TLS | Encrypted username + password | Validate with a `PROPFIND` before saving; support a per-account custom CA; refuse plain HTTP |
| CalDAV (Fastmail) | App password (MVP), OAuth later | Encrypted | Fastmail supports OAuth for native public clients without manual registration |
| Google | Plugin-side OAuth 2.0 **Desktop app** client: system browser + loopback redirect (`http://127.0.0.1:<port>`) with PKCE → plugin exchanges code and stores refresh token | Encrypted refresh token in the plugin credential vault; access token in memory | Headless: SSH port-forward, or paste the redirect URL back into the UI; Google device-code is not permitted for Calendar scopes |
| Microsoft | Plugin web OAuth with PKCE, or device code | Plugin holds refresh token | `common` tenant; admin consent for org accounts; renew subscriptions separately |
| ICS | None, or Basic/custom header | URL/credentials encrypted | Treat URL as a secret; never echo it in full |

**Google details.** The plugin uses a Google Cloud **Desktop app** OAuth client.
It starts a loopback listener, opens the system browser, validates `state`, and
exchanges the one-time code with the PKCE verifier; it then stores the refresh
token in the credential vault. The Desktop client secret is not treated as
confidential, and the loopback listener binds to `127.0.0.1` only. Access
tokens are cached in memory and refreshed proactively. On a headless host,
SSH-forward the callback port or paste the failed redirect URL into the web UI
(§3.3); Google device-code is not available for Calendar scopes. `invalid_grant`
means re-consent; never retry it in a loop. Because sensitive-scope apps in
Testing mode get 7-day refresh tokens, document that the OAuth consent screen
must be **In production** for reliable sync, even if the app is unverified and
shows a warning.

**Microsoft details.** Prefer plugin-brokered web OAuth when the plugin has a
browser-reachable HTTPS origin (Tailscale with a trusted certificate is ideal).
Otherwise use device code flow: the plugin displays
`https://microsoft.com/devicelogin` and a code in the web UI; it polls the token
endpoint until the user approves. Both flows yield a refresh token for
`Calendars.ReadWrite offline_access`. Do not use the implicit flow.

**Fastmail OAuth (post-MVP).** Fastmail supports OAuth for native clients
using a private-use redirect URI, and does not require manual client
registration for the open-public-client profile. If added, use the same
plugin-side loopback pattern as Google: the browser returns a code, the plugin
exchanges it.

**CalDAV OAuth (post-MVP).** Only useful for providers that support it. Keep
the generic connector on Basic auth; add an auth strategy interface rather
than branching inside the WebDAV client.

### 8.4 Secret storage

- Use envelope encryption: a master key from the OS keyring, TPM, or a
  deployment-provided secret; a per-account data key wrapped by it.
- Never store secrets in the same SQLite file as the event cache without
  encryption. Prefer a separate vault table or file.
- Redact `Authorization`, `Set-Cookie`, feed URLs, OAuth codes, and pasted
  redirect URLs from logs and diagnostics. Use the same redaction rules as the
  rest of the Hermes gateway's diagnostic log.
- Exclude vault files from backups unless the user opts in, and document that
  a restored backup without the master key cannot sync.
- Rotate the plugin's app session tokens on a schedule and on sign-out.

### 8.5 Consent and scopes

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

## 9. MVP order and two-way strategy

### 9.1 Recommendation

**Build in this order:**

| Phase | Deliverable | Why here | Exit criteria | Effort |
|---|---|---|---|---|
| 0 | Canonical store, profile/global DB split, mapping tables, iCalendar codec, recurrence/timezone engine, fake provider, tool API skeleton | Everything else depends on the model; catch timezone/recurrence bugs before any network code | Round-trip RFC 5545 fixtures; expansion passes DST tests; fake provider passes pull/push tests; mapping CRUD works | 1.5–2 wk |
| 1 | **ICS import** (read-only feeds) | Zero auth, zero write risk; proves parser, diffing, and UI; immediately useful (holidays, sports, shared feeds) | Import Google/Outlook/Apple published feeds; refresh honors ETag/interval; malformed feed does not break others | 1–1.5 wk |
| 2 | **CalDAV two-way** (iCloud, Fastmail, Nextcloud) | One connector covers three providers; standard ETags and sync-token; no app review; exercises the full conflict engine | Create/update/delete on all three; mapping create/remove; 412 recovery; full resync; recurrence exceptions; conflict inbox | 3–4 wk |
| 3 | ICS export/publish | Cheap, useful, no write path; lets users subscribe to agent-created plans from any calendar app | Stable UID export; token rotation; subscribers update rather than duplicate | 0.5–1 wk |
| 4 | **Google Calendar** two-way | High user value but high auth/verification friction; engine already proven | OAuth connect via the local web UI; Google-first default mapping; syncToken + 410 recovery; recurring instances; quota-safe polling | 2–3 wk |
| 5 | **Microsoft Graph** two-way | Work accounts; most auth and subscription complexity | Delta window + deltaLink recovery; subscription renewal; personal and work accounts | 3–4 wk |

**MVP = Phases 0–2** (plus Phase 3 if there is room): read-only feeds and
two-way CalDAV. That is roughly **6–8 engineer-weeks** with tests, and it
covers Apple, Fastmail, Nextcloud, and every ICS publisher without a single
OAuth review.

### 9.2 Why CalDAV before Google

- **Coverage per unit of work.** One connector reaches iCloud (the default for
  Apple users), Fastmail, Nextcloud, and self-hosted servers.
- **No app review.** Google's sensitive scopes require verification for public
  distribution; CalDAV needs an app password.
- **It forces the hard parts early.** WebDAV ETags, sync tokens, recurrence
  exceptions, and 412 recovery are the same problems Google and Graph have.
  Solving them on a protocol with a readable spec is easier than learning them
  through OAuth and quota errors.
- **Self-hosted alignment.** Hermes is a private-network, self-hosted product.
  Its audience skews toward people who run their own services and are more
  likely to have Nextcloud or Fastmail than a Google Workspace domain, though
  this should be checked against actual users (see §9.2's swap rule).

**When to swap.** If user research shows >50% of target users are Google-only,
move Google to Phase 2 and CalDAV to Phase 3. The engine, store, conflict
rules, and tool API are identical; only the adapter and auth broker move. Do
not build both in parallel: two half-finished connectors are worse than one
finished one.

### 9.3 Two-way vs one-way

| Substrate | Direction | Recommendation |
|---|---|---|
| ICS import | One-way (pull) | Always read-only; no writeback, ever |
| ICS export | One-way (push) | Read-only published feed; no inbound writes |
| CalDAV | Two-way | Yes, but writes opt-in per account/collection and limited to the §4.5 field set |
| Google | Two-way | Same engine and limits |
| Graph | Two-way | Same engine and limits |
| Mapping topology | Mixed per mapping | One local calendar → N remotes; at most one `two_way` per local calendar; additional mappings are `pull` or `push` |
| Provider ⇄ provider | None | No mirroring in the MVP; revisit only with loop budgets and explicit user intent |

Read-only is a first-class mode, not a failure. A user may want the agent to
see their calendar but never change it. Every account starts read-only; a
separate "allow changes" toggle enables the outbox. This also gives a safe
rollout path: ship Phase 2 read-only, then enable writes behind a flag.

### 9.4 What is deliberately deferred

- Attendee invites and RSVP (first post-MVP).
- Free/busy federation and `getSchedule`.
- Google/Graph webhooks in the default deployment (polling is the baseline).
- Fastmail/CalDAV OAuth.
- Cross-provider mirroring.
- Shared-calendar ACL management.
- `VTODO` and reminders-as-tasks.
- Attachments and conference data.
- Multi-user tenancy.

### 9.5 Success metrics

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

## 10. Agent tool surface and safety

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

## 11. Testing and operations

### 11.1 Test strategy

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

### 11.2 Observability

Per account and collection, expose:

- last successful pull/push, cursor age, outbox depth, oldest pending op;
- counts by error class (`auth`, `rate_limit`, `conflict`, `parse`, `network`);
- conflict rate and auto-resolution rate;
- initial sync duration and event count;
- token refresh failures and reauth prompts.

Trace one sync cycle with a correlation ID. Redact secrets and event bodies.
Alert on "auth failed", "cursor invalid repeatedly", and "outbox older than 1
hour".

### 11.3 Recovery

- **Rebuild from provider:** drop the cursor and cache for one collection, keep
  dirty local events, full sync, re-merge. Safe at any time.
- **Restore a revision:** reapply a snapshot through the normal outbox path, so
  the provider sees a normal edit.
- **Resolve a stuck outbox:** inspect the last error, refresh the ETag, retry,
  or discard with a revision backup.
- **Disconnect:** revoke, delete credentials, keep or purge the local cache per
  user choice.

---

## 12. Open questions

1. **Process model.** The doc assumes a Python plugin running in or under the
   Hermes gateway. Confirm whether it runs in-process or in a per-profile host
   process, because that decides whether the shared global DB or the
   hub-service fallback is appropriate.
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
8. **Global store placement.** Is
   `$HERMES_HOME/plugin-data/hermes-calendar/global/` writable and shared in
   every supported deployment (containers, per-profile volumes, hosted
   gateways)? If not, do we ship the hub-profile service or wait for a
   gateway-wide storage API?
9. **Global data encryption.** If the global DB is encrypted at rest, how is
   the key shared across profiles? Per-profile keys cannot open a shared file;
   the realistic options are OS full-disk encryption, a gateway keyring, or
   the hub service.
10. **Single primary mapping.** Is at most one `two_way` mapping per local
    calendar acceptable, or do users need two providers to be authoritative
    simultaneously? The recommendation is single-primary for the MVP.
11. **Global mapping ownership.** Should the profile that created a global
    mapping own it permanently, or should ownership be transferable? What
    happens to the mapping when that profile is deleted or its account is
    re-authenticated in another profile?
12. **Global ACLs.** R2 says every profile can read and write the global
    calendar. Confirm there is no need for read-only profiles, per-event
    ownership, or moderation.
13. **Cross-mapping duplicates.** Should the product offer an explicit
    "link these events" feature for the same event copied across remotes, or
    is keeping separate copies acceptable?
14. **Backup and uninstall.** Should uninstalling the plugin delete the shared
    global DB, and how is it included in gateway backups when each profile has
    its own database?

---

## 13. References

- RFC 4791 — CalDAV: <https://www.rfc-editor.org/rfc/rfc4791>
- RFC 4918 — WebDAV: <https://www.rfc-editor.org/rfc/rfc4918>
- RFC 5545 — iCalendar: <https://www.rfc-editor.org/rfc/rfc5545>
- RFC 6578 — Collection Synchronization for WebDAV: <https://www.rfc-editor.org/rfc/rfc6578>
- RFC 6638 — CalDAV Scheduling: <https://www.rfc-editor.org/rfc/rfc6638>
- RFC 7986 — New iCalendar Properties: <https://www.rfc-editor.org/rfc/rfc7986>
- RFC 7636 — PKCE: <https://www.rfc-editor.org/rfc/rfc7636>
- RFC 8252 — OAuth 2.0 for Native Apps: <https://www.rfc-editor.org/rfc/rfc8252>
- Google Calendar API — sync: <https://developers.google.com/workspace/calendar/api/guides/sync>
- Google Calendar API — push: <https://developers.google.com/workspace/calendar/api/guides/push>
- Google Calendar API — quota: <https://developers.google.com/workspace/calendar/api/guides/quota>
- Google Workspace agent-tool quotas (May 2026): <https://developers.google.com/workspace/tools-safety>
- Google OAuth 2.0 — desktop/native apps (PKCE + loopback): <https://developers.google.com/identity/protocols/oauth2/native-app>
- Google OAuth 2.0 — refresh-token expiration (Testing mode): <https://developers.google.com/identity/protocols/oauth2#expiration>
- Google OAuth 2.0 — device flow, allowed scopes (no Calendar): <https://developers.google.com/identity/protocols/oauth2/limited-input-device#allowedscopes>
- Google OAuth 2.0 — out-of-band flow deprecation: <https://developers.google.com/identity/protocols/oauth2/resources/oob-migration>
- Google unverified apps / verification FAQ: <https://support.google.com/cloud/answer/9110914>
- Google CalDAV guide: <https://developers.google.com/workspace/calendar/caldav/v2/guide>
- Microsoft Graph — delta query: <https://learn.microsoft.com/graph/delta-query-overview>
- Microsoft Graph — event delta: <https://learn.microsoft.com/graph/api/event-delta>
- Microsoft Graph — change notifications: <https://learn.microsoft.com/graph/change-notifications-overview>
- Apple — app-specific passwords: <https://support.apple.com/en-us/102654>
- Fastmail — app passwords: <https://www.fastmail.help/hc/en-us/articles/360058752854>
- Fastmail — OAuth for developers: <https://www.fastmail.com/dev/>
- Hermes Plugin API research notes: `docs/research/hermes-plugin-api.md` on
  branch `feature/research-hermes-api` (plugin state, `plugin_data_dir()`,
  `plugin_db()`, profile scoping)
- Nextcloud — CalDAV with app passwords: <https://help.nextcloud.com/t/caldav-and-oauth2-oidc/214815>
