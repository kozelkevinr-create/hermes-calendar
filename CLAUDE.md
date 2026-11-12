# CLAUDE.md

Guidance for AI agents and humans working in this repository. Read this before changing
code; it encodes conventions that CI and Hermes both enforce.

## What this repository is

A local-first calendar plugin for Hermes Agent. The Hermes plugin contract is
`plugin.yaml` + `__init__.py::register(ctx)` in the repository root; the implementation is
the `hermes_calendar` Python package. Design documents in `docs/` are the source of truth
for their areas.

| Area | Source of truth |
|---|---|
| Hermes plugin API, hooks, scheduling, notifications | `docs/research/hermes-plugin-api.md` |
| Event model, SQLite schema, recurrence, change log | `docs/design/core.md` |
| Sync engine and remote providers | `docs/design/sync.md` |
| Reminder scheduling and delivery | `docs/design/app-notifications.md` |

Read the relevant design document before implementing its area. If the design is wrong,
fix the design in the same PR — do not silently diverge.

## Branch workflow (required)

Promotion is strictly one-way: **feature → test → stage → main**. `main`, `stage`, and
`test` are protected; never commit or push to them directly.

```text
feature/<slug>  ──PR (squash)──▶  test  ──PR (merge)──▶  stage  ──PR (merge)──▶  main
```

### Feature work

```sh
git fetch origin
git switch test
git pull --ff-only
git switch -c feature/<slug>       # e.g. feature/reminder-engine
```

- Branch from **`test`**, never from `main`, `stage`, or another feature branch.
- Keep one logical change per branch. Split unrelated work.
- Rebase on `test` to resolve drift; do not merge `test` into your feature branch unless
  resolving a conflict requires it.
- Open a pull request into **`test`**. Title it with a Conventional Commit prefix
  (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`).
- Feature PRs are **squash-merged**; delete the branch after merge.
- Merge only when CI is green and the PR has review approval. Agents do not merge their
  own PRs or promote branches without explicit coordinator approval.

### Promotion

- `test → stage`: pull request with a **merge commit**, no code changes. It ships what has
  already passed integration. Use a `release:` or `chore(release):` title.
- `stage → main`: pull request with a **merge commit**, no code changes. Smoke-test the
  staged build first.
- Never cherry-pick or rewrite history on a protected branch. Promotions must contain the
  exact commit range that was tested.
- Tag `main` after promotion: `vMAJOR.MINOR.PATCH`.

### Hotfixes

```sh
git switch main
git pull --ff-only
git switch -c hotfix/<slug>
```

PR the hotfix into `main`, then immediately back-merge `main` into `test` and `stage`
(fast-forward or merge commit) so the fix is not lost in the next promotion.

### Branch protection expectations

- No direct pushes; pull request required.
- Required checks: `lint`, `typecheck`, `test (3.11)`, `test (3.12)`, `test (3.13)`,
  `plugin-manifest`.
- One approving review; conversations resolved; branch up to date before merge.
- No force-pushes, no deletions of protected branches.

## Commits

Conventional Commits, imperative mood, ≤72-character subject:

```text
feat(reminders): claim due reminders with a lease
fix(sync): keep remote etag on conflict
docs: add sync conflict matrix
test(db): cover migration from schema v1
```

Body explains **why**, not what. Reference the task or design section when useful.

## Quality gates

CI runs on every PR and on pushes to `feature/**`:

```sh
make lint          # ruff check . + ruff format --check .
make typecheck     # mypy --strict
make test          # pytest + coverage (fail-under 90)
make plugin-validate
```

Run `make check` before requesting review. Set `HERMES_VALIDATE=1` (with Hermes on `PATH`)
to add the real `hermes plugins validate .` gate; CI always runs the offline subset.
Do not weaken a gate to make a change pass
(add `# noqa` / `# type: ignore` only with a reason on the same line).

### Testing expectations

- Unit tests are fast, offline, deterministic, and do not require Hermes installed.
- Integration tests are marked `@pytest.mark.integration` and opt-in.
- Fake external systems at the boundary: a fake `hermes` executable for `hermes send`, a
  fake HTTP server for ntfy/Pushover/Web Push, an injected clock for scheduling.
- Reminder concurrency is mandatory coverage: two tickers claiming the same due row must
  produce exactly one user-visible delivery.
- Time tests must cover DST spring-forward/fall-back, `UNTIL` inclusivity, `EXDATE`, and
  all-day boundaries.

## Hermes plugin rules (non-negotiable)

Derived from `docs/research/hermes-plugin-api.md`:

1. **Register, do not monkey-patch.** Expose tools/hooks/commands through `ctx` inside
   `register(ctx)`. Never import or rebind Hermes internals — private core imports fail
   catalog review and break on upgrade.
2. **Declarations must match reality.** Every tool/hook added in `register()` must appear
   in `plugin.yaml` (`provides_tools` / `provides_hooks`) or `hermes plugins validate`
   fails on drift.
3. **Profile-scoped state.** Key state by the profile/home passed to the hook; never cache
   "the" home directory or read ambient `os.environ` for credentials. A shared gateway
   serves several profiles.
4. **Never write the install directory.** `hermes plugins update` and `remove` replace it.
   Use `ctx.state`, `plugin_data_dir()`, or `plugin_db()`.
5. **Capabilities are fail-closed and are not a sandbox.** Probe with
   `ctx.has_capability(...)` and degrade; do not assume consent. Only request what you
   enforce.
6. **Background work must survive.** Prefer a single `hermes cron` safety net plus an
   in-process ticker, guarded by a DB lease. `ctx.post_message` is turn-scoped and is
   refused from background threads; background delivery needs `ctx.inject_message` plus
   consent, or cron `deliver:` targets.
7. **Secrets are references, not values.** Store `env:`/`keyring:`/Hermes references;
   never put secrets in SQLite, logs, message bodies, or the repository.
8. **No shell strings.** Use `subprocess.run([...])`, never `shell=True`; calendar text is
   untrusted and must never reach a shell or template.
9. **Validate before shipping.** `hermes plugins validate --json .` and
   `hermes plugins doctor --ci .` when Hermes is available; the offline
   `scripts/check_manifest.py` runs in CI.

## Code conventions

- Python 3.11+, standard library first. Add a dependency only with a design rationale.
- Full type annotations; `mypy --strict` is enforced.
- `from __future__ import annotations` at the top of every module.
- Public functions and modules get docstrings; explain invariants and failure modes.
- Keep SQL in `hermes_calendar/db`; no other package writes SQL directly.
- Keep Hermes-specific code in `hermes_calendar/hermes` behind narrow interfaces so the
  core is testable without Hermes.
- Time is stored as UTC instants (epoch seconds in SQLite, RFC 3339 at boundaries) plus a
  local wall clock and IANA zone where recurrence requires it.
- Errors returned to the model are JSON strings; exceptions are for programmer errors.

## Working as a dispatched agent

- Work only in your assigned worktree/branch; do not switch to or modify another task's
  branch or design document.
- Do not merge, push protected branches, tag, or promote. Report completion to the
  coordinator and let it decide.
- If a design decision is ambiguous, ask the coordinator instead of inventing policy.
- Leave the tree clean and the branch committed; include real file paths in your report.
