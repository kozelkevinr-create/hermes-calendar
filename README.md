# hermes-calendar

A local-first calendar plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
events, recurring series, reminders, and two-way calendar sync — with the data staying
on your machine.

> **Status: scaffold.** The repository skeleton, plugin contract, lint/type/test
> configuration, and CI are in place. Product code lands incrementally behind the design
> documents listed below.

## Why

Hermes already owns your assistant, your messaging channels, and your credentials. A
calendar that lives inside Hermes can remind you through channels you already read, without
a second account or a second notification stack. The design goals are:

- **Local-first.** SQLite on disk; no background server required for the core model.
- **Hermes-native.** One `plugin.yaml`, `register(ctx)`, and `hermes send` for delivery.
- **Correct about time.** UTC instants plus local wall clock and IANA zone, so DST never
  moves a 09:00 meeting.
- **Honest about delivery.** Every reminder produces an in-app record first, then routes
  to optional external channels with a durable delivery log.

## Architecture at a glance

| Layer | Package | Responsibility |
|---|---|---|
| Plugin contract | `plugin.yaml`, `__init__.py`, `hermes_calendar/plugin.py` | Register tools, hooks, commands with Hermes |
| Core model | `hermes_calendar/models`, `hermes_calendar/db` | Canonical events, SQLite schema, change log |
| Recurrence | `hermes_calendar/recurrence` | RFC 5545 expansion, DST, overrides |
| Reminders | `hermes_calendar/reminders` | Claim/lease engine, routing, delivery adapters |
| Sync | `hermes_calendar/sync` | Remote adapters and conflict handling |
| Web UI | `hermes_calendar/ui` | Local web UI / PWA |
| Hermes adapters | `hermes_calendar/hermes` | `ctx` surfaces, profiles, `hermes send`, cron |
| CLI | `hermes_calendar/cli.py` | `hermes-calendar` entry point and diagnostics |

## Design documents

The design is developed on sibling feature branches and merged into `test` before
implementation. These are the source of truth for their areas:

| Document | Scope |
|---|---|
| `docs/research/hermes-plugin-api.md` | Hermes plugin contract, hooks, capabilities, scheduling, notifications |
| `docs/design/core.md` | Event model, SQLite schema, recurrence, change log |
| `docs/design/sync.md` | Sync engine, remote providers, conflict handling |
| `docs/design/app-notifications.md` | Reminder scheduling and notification delivery |

If a design and this README disagree, the design document wins; update the README in the
same PR.

## Requirements

- Python 3.11+
- Hermes Agent 0.23+ (for plugin validation and `hermes send`)
- SQLite 3.38+ (uses `unixepoch()`, JSON1, partial indexes)

The test suite and offline manifest check run without Hermes installed.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"

make check          # lint + format check + typecheck + tests + manifest
make test           # pytest with coverage
make lint           # ruff check + format check
make typecheck      # mypy --strict
make plugin-validate  # offline manifest checks by default
make plugin-validate HERMES_VALIDATE=1  # real `hermes plugins validate .`
```

`pre-commit install` wires ruff and mypy into local commits.

### CLI

```sh
hermes-calendar --version
python -m hermes_calendar --version
```

Subcommands (`reminders tick`, `reminders doctor`, `db migrate`) are added by their
subsystems. Diagnostics go to stderr, `--json` is opt-in, and exit codes are the API.

## Installing into Hermes

```sh
hermes plugins install <owner>/hermes-calendar --enable
hermes plugins list
hermes plugins validate .
```

Hermes installs plugins **disabled** by default; enabling is the trust decision. Pin
production installs to a 40-character commit SHA with `--ref`.

Plugin state belongs in the Hermes profile's plugin data directory (`plugin_data_dir()` /
`plugin_db()`), never in the install directory: `hermes plugins update` replaces it.

## Repository layout

```text
.
├── plugin.yaml                 # Hermes manifest (name, version, surfaces)
├── __init__.py                 # Hermes entry point -> hermes_calendar.plugin.register
├── hermes_calendar/            # implementation package
│   ├── cli.py                  # `hermes-calendar` entry point
│   ├── plugin_storage.py       # generic per-profile/global data dirs + SQLite open helper
│   ├── db/                     # SQLite connection + schema + repositories
│   ├── models/                 # canonical event model
│   ├── recurrence/             # RRULE expansion
│   ├── reminders/              # scheduling and delivery
│   ├── sync/                   # remote calendar adapters
│   ├── ui/                     # local web UI
│   └── hermes/                 # Hermes-specific adapters
├── tests/
│   ├── unit/                   # fast, no network, no Hermes
│   └── integration/            # opt-in: network / credentials / real Hermes
├── scripts/check_manifest.py   # offline subset of `hermes plugins validate`
└── .github/workflows/ci.yml
```

## Branch workflow

`main` is production, `stage` is the release candidate, `test` is integration, and every
change starts as a `feature/*` branch cut from `test`. Promotion is always a pull request:
`feature/* → test → stage → main`. Details, including hotfixes and required checks, are in
[CLAUDE.md](CLAUDE.md).

## License

MIT. See [LICENSE](LICENSE).
