# Documentation

Design work is developed on `feature/*` branches and merged into `test` before
implementation. Documents state their own `Status` and owner; update that header when a
decision changes.

| Path | Contents |
|---|---|
| `research/` | External API and platform findings |
| `design/` | Feature designs that implementation follows |
| `adr/` | Architecture decision records (add one when a decision is expensive to reverse) |

| Document | Scope |
|---|---|
| `research/hermes-plugin-api.md` | Hermes plugin contract, hooks, capabilities, scheduling, notifications |
| `design/core.md` | Event model, SQLite schema, recurrence, change log |
| `design/sync.md` | Sync engine, remote providers, conflict handling |
| `design/app-notifications.md` | Reminder scheduling and delivery |

## Conventions

- One document per area; link rather than duplicate.
- State assumptions and non-goals explicitly.
- Record open questions with a default decision so implementation can proceed.
- If code and design disagree, the design wins until an ADR says otherwise.
