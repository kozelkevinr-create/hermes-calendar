# Decisions

Implementation decisions taken where the design documents leave room, one line each.
Date, decision, why.

- 2026-11-12 — Both stores use the same v1 schema and seed a local source `src_local`; only the global store seeds its one `cal_global` calendar. Why: `core.md` §5.2 requires exactly one global calendar but leaves profile calendar creation to the user, while a local source is the minimum needed for `create_calendar`.
- 2026-11-12 — Schema v1 omits `custom_timezones`, the instance cache, sync tables, FTS, and notification tables. Why: the M1 task explicitly scopes those out and they can be added by later additive migrations.
- 2026-11-12 — `create_event` takes a frozen `EventDraft`; `update_event` takes a full `Event` plus `expected_rev` and replaces mutable fields. Why: one aggregate-shaped API keeps `source_id`/`scope` consistent with the calendar and makes frozen dataclasses usable with `dataclasses.replace`.
- 2026-11-12 — `delete_event` takes an optional `expected_rev` and treats a repeat delete as a no-op. Why: the task requires CAS on update; idempotent delete is safer for retries and the row remains addressable as a tombstone.
- 2026-11-12 — `list_events` returns non-recurring rows (timed and all-day) by UTC sort keys and excludes recurring masters and overrides. Why: M1 skips recurrence expansion, and all-day rows already carry stable UTC-midnight keys.
- 2026-11-12 — A profile store's identity is its profile directory name from `profile_dir().parent.parent.name`; opening an existing store verifies its scope but not its `store_id`. Why: `core.md` §4.1 defines identity by directory name and says a rename is a store-id migration, so a hard failure on drift would block that migration.
- 2026-11-12 — Global-store events use `draft.owner_profile_id` or the write `actor` as creator; profile-store events always take the calendar owner. Why: `core.md` §4.6 requires the creating profile in the shared store, while profile ownership is fixed by the store.
- 2026-11-12 — Keep the shared `plugin_storage.open_db` pragma stack, including its 5 s `busy_timeout`, rather than overriding it to the design's 10 s. Why: the task requires that helper to own connection setup, and M1 write transactions are short; the helper remains the one place to tune it.
- 2026-11-12 — Tool datetimes without an offset default to `HERMES_TIMEZONE`, then the profile `config.yaml` top-level `timezone` key, then UTC; the tool also accepts an explicit `timezone` argument. Why: Hermes resolves the same order, but importing its private clock module would break the plugin contract, and reading one top-level YAML key keeps the plugin dependency-free.
