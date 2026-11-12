# Decisions

Short, dated records for choices the design left open. One line per decision:
date, decision, and why.

- 2026-11-12 — Use `python-dateutil>=2.9` for the RFC 5545 grammar instead of a hand-rolled parser; it is pure Python and already present in the Hermes runtime (2.9.0.post0 in the installed environments), and the design explicitly asks for a vetted library (§6.2).
- 2026-11-12 — Give `hermes_calendar.recurrence` explicit `MasterEvent`/`OverrideEvent` dataclasses rather than accepting SQLite rows; this keeps the module pure, typed, and free of database imports, with mapping left to the storage layer.
- 2026-11-12 — Resolve floating timed events in UTC during expansion; `expand()` has no calendar default zone, so this preserves the wall clock without inventing a caller-specific zone. Callers that have a calendar zone pass it as `start_tz`.
- 2026-11-12 — Expand an RRULE-less master as `{DTSTART} ∪ RDATE − EXDATE` rather than returning only the master; this follows the normative recurrence-set definition in §6.1 and makes RDATE-only series work.
- 2026-11-12 — Filter all-day occurrences by the UTC dates of the supplied window; this matches the date-based query in §6.8 and keeps all-day dates free of time-zone shifts.
- 2026-11-12 — Accept UTC window bounds as either timezone-aware datetimes or epoch seconds; epoch seconds are what SQLite stores, and normalizing once at the public boundary keeps the rest of the engine in datetimes.
- 2026-11-12 — Apply overrides only to occurrences the RRULE/RDATE window generated; an override moved into the window from an original occurrence after the window is not discovered, which avoids scanning an unbounded future for a rare case.
