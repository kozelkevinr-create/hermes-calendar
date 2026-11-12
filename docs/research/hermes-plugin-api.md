# Hermes Plugin API — Research Notes

Research-only notes on how Hermes Agent plugins are packaged, registered, gated and
scheduled. Collected 2026-11-12 against **Hermes Agent v0.23.0 (2026.9.24, commit
`2bd378ed1b8d`)**.

Everything below is derived from the installed Hermes tree, the shipped plugin
authoring docs, and the CLI. Where a claim comes from a specific file or command the
source is cited inline. Internal modules that are *not* part of the documented plugin
contract are called out explicitly.

---

## 0. TL;DR

- A plugin is a directory with `plugin.yaml` + `__init__.py::register(ctx)`. There is
  no global `PLUGIN_API_VERSION`; compatibility is a **behavior contract** (additive
  hooks/context, never-removed `ctx` methods).
- Tools and hooks are registered through `ctx.register_tool()` /
  `ctx.register_hook()` inside `register(ctx)`. Registration is tracked and undone on
  unload/reload.
- Privileged surfaces are declared under `capabilities:` and consented once. Capability
  checks are **fail-closed** but are **not a sandbox** — plugin code runs in-process
  (or in a plugin host process) with the user's permissions.
- Portable **Agent Plugins v1** packages (`plugin.json` + `skills/` + `mcp.json`) are a
  compatibility adapter, not a replacement for native plugins; they install disabled
  and grant full trust when enabled.
- Installs come from the curated catalog, a Git URL, or `owner/repo` shorthand, with an
  optional immutable `--ref <40-hex SHA>`. Provenance lives in
  `~/.hermes/plugins/.install-metadata.json`, never in the plugin tree.
- A plugin can schedule background work three ways: `ctx.spawn_task()` (supervised
  asyncio), a **cron scheduler provider** (`kind: cron provider`, selected by
  `cron.provider`), or by creating ordinary cron jobs / automation blueprints. User
  notifications go through `ctx.post_message`, `ctx.inject_message`, platform actions,
  cron `deliver:` targets, an approval transport, or shell hooks.

---

## 1. Sources and how they were gathered

Hermes is installed on the `docker-host` machine and its CLI is an SSH wrapper
(`/home/<user>/.local/bin/hermes` runs `ssh docker-host ...`), so library sources
were read over SSH.

Files read:

| Path (on `docker-host`) | Used for |
|---|---|
| `~/.hermes/plugins/orca-status/plugin.yaml` | Real minimal manifest (also present locally at `/home/<user>/.hermes/plugins/orca-status/`) |
| `~/.hermes/plugins/orca-status/__init__.py` | Real register-only plugin, hook payload shaping |
| `~/.hermes/hermes-agent/website/docs/developer-guide/plugins/index.md` | Canonical authoring guide (2,619 lines) |
| `~/.hermes/hermes-agent/plugins/AGENTS.md` | Plugin kinds, discovery systems, compat contract, host boundary |
| `~/.hermes/hermes-agent/hermes_cli/plugins.py` | `PluginContext` implementation, `VALID_HOOKS`, tool/hook/command registrars |
| `~/.hermes/hermes-agent/hermes_cli/plugins_manifest.py` | Manifest parser, known fields, `kind` values, manifest v2 |
| `~/.hermes/hermes-agent/hermes_cli/plugin_capabilities.py` | Capability registry + consent state |
| `~/.hermes/hermes-agent/hermes_cli/plugins_content.py` | `register_automation_blueprint`, `register_skill` |
| `~/.hermes/hermes-agent/hermes_cli/plugin_messages.py`, `plugins_injection.py` | Notification/injection routing and refusals |
| `~/.hermes/hermes-agent/plugins/cron_providers/__init__.py` | Cron scheduler provider discovery |
| `~/.hermes/hermes-agent/plugins/cron_providers/chronos/plugin.yaml` | Example provider manifest |
| `~/.hermes/hermes-agent/cron/scheduler_provider.py` | `CronScheduler` ABC and scope of provider control |
| `~/.hermes/hermes-agent/cron/scheduler.py` | `create_job_with_scheduler_registration()` |
| `~/.hermes/hermes-agent/cron/jobs.py` | `create_job()`, `update_job()` (internal) |
| `~/.hermes/hermes-agent/website/docs/developer-guide/plugins/catalog-submission.md` | Catalog admission and pin rules |
| `~/.hermes/hermes-agent/website/docs/user-guide/features/plugins.md` | Install/enable/update behavior |
| `~/.hermes/hermes-agent/website/docs/user-guide/features/plugin-catalog.md` | Catalog entry schema, install prompts |
| `~/.hermes/hermes-agent/website/docs/user-guide/features/cron.md` | Cron delivery targets and notification flags |
| `~/.hermes/plugins/hermes-dreaming/plugin.json` | Real portable Agent Plugins v1 package |

Commands run:

```bash
hermes --version
hermes plugins --help
hermes plugins install --help
hermes plugins doctor --help
hermes plugins validate --help
hermes plugins capabilities
hermes plugins browse
hermes plugins list
hermes plugins show homeassistant
hermes plugins validate ~/.hermes/hermes-agent/plugins/security-guidance
hermes plugins validate /tmp/orca-status-validate/orca-status   # copy of ~/.hermes/plugins/orca-status
```

Observed tool versions / outputs are quoted verbatim in §3, §6, and §12.

---

## 2. Discovery, enablement, and lifecycle

From `plugins/AGENTS.md` and the authoring guide (§*Debugging plugin discovery*):

- **General plugin search order** (`PluginManager`, `hermes_cli/plugins.py`):
  1. bundled `plugins/<name>/` in the Hermes checkout,
  2. user `$HERMES_HOME/plugins/<name>/` (normally `~/.hermes/plugins/`),
  3. project `./.hermes/plugins/` (only when `HERMES_ENABLE_PROJECT_PLUGINS`),
  4. pip entry points (`hermes_agent.plugins`).

  Conflicts are **later-wins** for general plugins (the reverse of memory providers,
  which are bundled-first).
- Layout is flat or **one** category level: `~/.hermes/plugins/<name>/plugin.yaml` or
  `~/.hermes/plugins/<category>/<name>/plugin.yaml`. Deeper nesting is ignored.
- **Opt-in by default.** Discovery finds a plugin so it appears in `hermes plugins list`,
  but nothing loads until the plugin key is in `plugins.enabled`. Memory providers are
  auto-detected as `kind: exclusive` and activated by `memory.provider` instead.
- Discovery runs as a side effect of importing `model_tools.py`; mid-run load paths call
  `discover_plugins(force=True)`. `PluginManager.on_plugin_loaded` fires from that sweep
  with an activation summary: hooks are live now, tools/prompt sections next session,
  `deferred.mcp_servers` after `mcp.reload`.
- `register(ctx)` is called exactly once at load. If it raises, the loader catches the
  exception, records it on the plugin and **disables that plugin**; Hermes continues.
- Loads are time-bounded by `plugins.load_timeout_seconds`; a load that overruns is
  abandoned and late registrations from the orphaned thread are dropped
  (`PluginContext._abandon_load`).
- `plugins.isolation: host` runs every non-bundled plugin in a per-profile host process
  with a `RemotePluginContext`; `plugin_isolation.HOST_UNSUPPORTED_CTX_METHODS` lists the
  live gateway/adapter objects that cannot cross. `hermes plugins validate` / `show`
  report the mode.

Useful diagnostics: `HERMES_PLUGINS_DEBUG=1 hermes plugins list`,
`hermes logs --level WARNING | grep -i plugin`, and `hermes plugins doctor [path|id] --ci`.

---

## 3. `plugin.yaml` schema

### 3.1 Minimal manifest

```yaml
name: calculator
version: 1.0.0
description: Math calculator — evaluate expressions and convert units
provides_tools:
  - calculate
  - unit_convert
provides_hooks:
  - post_tool_call
```

(`website/docs/developer-guide/plugins/index.md`, Step 2.)

`name`, `version`, `description` are required (the validator's `manifest fields` check).
Unknown fields are ignored with a warning — that is deliberate forward compatibility.

### 3.2 Recognized fields

From `_KNOWN_MANIFEST_FIELDS` in `hermes_cli/plugins_manifest.py`:

`name`, `version`, `description`, `author`, `requires_env`, `provides_tools`,
`provides_hooks`, `kind`, `hooks`, `label`, `optional_env`, `platforms`,
`external_dependencies`, `pip_dependencies`, `provides_browser_providers`,
`provides_web_providers`, `manifest_version`, `api_version`, `requires_plugins`,
`python_dependencies`, `config_schema`, `license`, `homepage`, `tags`, `capabilities`,
`emits`, `listens`, `hermes`, `depends`, `requires_hermes`, `python_runtime`,
`provides_locales`, `requires_auth`, `oauth`, `config_keys`.

Notes:

- `provides_tools` / `provides_hooks` are **declarations, not the registry**. For a
  user-installed plugin, everything `register()` registers loads whether declared or
  not. The lists drive `hermes plugins validate`'s drift check, catalog/dashboard chips
  and tool-name search, the dashboard "needs auth" hint, and — for bundled
  `kind: platform` plugins only — whether `tools.py` loads in CLI/TUI sessions.
- `hooks` is accepted as an alias for `provides_hooks`.
- `kind` ∈ `standalone`, `backend`, `exclusive`, `platform`, `model-provider`
  (`_VALID_PLUGIN_KINDS`). Unknown values warn and fall back to `standalone`.
  `standalone` is the default for general plugins; gateway adapters need `platform`.
- `requires_env` gates loading. Simple form is a list of names; rich form is
  `{name, description, url, secret}` and is prompted at install.
- `requires_hermes` is a version constraint (e.g. `>=0.19`) enforced before import:
  an install or update on an older Hermes is refused rather than loaded.
- `emits` / `listens` are reserved names describing plugin events; `hermes plugins show`
  prints `Emits:` / `Listens:` (observed `(none)` for `homeassistant`).
- `python_dependencies` (or a sibling `pyproject.toml`, which wins) triggers a Python
  dependency consent prompt; `python_runtime: external` opts a sidecar plugin out of
  Hermes-managed dependency resolution.
- Nothing to load is an error: `hermes plugins validate` rejects a `plugin.yaml` with no
  `__init__.py`, `desktop/plugin.js`, or `plugin.json` beside it.

### 3.3 Manifest v2 (additive, all fields optional)

`manifest_version: 2` is a file-format version, independent from `api_version` (runtime
surface generation). A v1 manifest stays supported forever. Table from the authoring
guide (§*Manifest v2 reference*):

| Field | Type | Meaning |
|---|---|---|
| `manifest_version` | int | File-format version; absent = 1, current max 2 |
| `api_version` | int | Runtime plugin API generation targeted (separate axis) |
| `requires_plugins` | list | Advisory inter-plugin deps (`- id: other`, optional `version_range`); load order is topologically sorted, cycles warn |
| `python_dependencies` | list[str] | PEP 508 requirements; consent before admission |
| `python_runtime` | str | `external` = plugin owns its interpreter/venv |
| `config_schema` | mapping | JSON-schema-ish settings description; drives the Desktop settings form |
| `config_keys` | list | Dotted core config paths the plugin reads (≥2 segments; cannot mask Hermes-defined paths) |
| `license` | str | SPDX id |
| `homepage` | str | Project URL |
| `tags` | list[str] | Discovery tags |
| `provides_locales` | list | Language packs (`locales/<id>[.tui\|.desktop].yaml`) |
| `requires_auth` | list[str] | Providers whose Hermes-managed sign-in is used (today `openai-codex`) |
| `oauth` | list[mapping] | OAuth clients Hermes owns (`id`, `authorize_url`, `token_url`, `scopes`, `client_id_env`, optional `client_secret_env`, `hosts`, `authorize_params`) |

```yaml
name: my-plugin
version: 1.2.0
manifest_version: 2
api_version: 1
license: MIT
homepage: https://github.com/owner/my-plugin
tags: [gateway, demo]
requires_plugins:
  - id: other-plugin
    version_range: ">=1.0,<2"
python_dependencies:
  - "somepkg>=1.0,<2"
config_schema:
  api_url: {type: str, default: "", description: "Service endpoint"}
```

### 3.4 Worked example: `orca-status`

`~/.hermes/plugins/orca-status/plugin.yaml` (verbatim, 17 lines):

```yaml
# Managed by Orca. Do not edit; changes may be overwritten.
name: orca-status
version: 1.0.0
description: "Reports Hermes Agent lifecycle events to Orca."
author: "Orca"
kind: standalone
provides_hooks:
  - on_session_start
  - pre_llm_call
  - post_llm_call
  - pre_tool_call
  - post_tool_call
  - pre_approval_request
  - post_approval_response
  - on_session_end
  - on_session_finalize
  - on_session_reset
```

This is the smallest useful manifest: no tools, no capabilities, no dependencies. Its
`__init__.py` (177 lines) is a hook-only observer: it builds a bounded, JSON-safe payload
(`_jsonable`, depth/items/node/string caps) from selected kwargs per event
(`SELECTED_KEYS`), then POSTs to `http://127.0.0.1:$ORCA_AGENT_HOOK_PORT/hook/hermes`
with `X-Orca-Agent-Hook-Token`, 0.75 s timeout, best-effort. `register(ctx)` is:

```python
def register(ctx: Any) -> None:
    for event_name in EVENTS:
        ctx.register_hook(event_name, _make_hook(event_name))
```

Two observations relevant to any observer plugin:

- It imports `agent.session_client_env.bound_client_env` (guarded by `ImportError`) —
  that is an **internal** module, flagged by `validate` as a private core import (§12).
- It resolves per-pane env through `bound_client_env()` rather than `os.environ`, because
  a shared gateway process serves several panes/profiles. This mirrors the documented
  rule that lifecycle hooks fire under the owning profile's scope and must not cache
  "the" home or read ambient `os.environ` for credentials (`plugins/AGENTS.md`).

---

## 4. How tools are registered

Signature (`PluginContext.register_tool`, `hermes_cli/plugins.py`):

```python
ctx.register_tool(
    name: str,
    toolset: str,
    schema: dict,
    handler: Callable,
    check_fn: Callable | None = None,
    requires_env: list | None = None,
    is_async: bool = False,
    description: str = "",
    emoji: str = "",
    override: bool = False,
) -> PluginRegistration | None
```

Behavior:

- **`schema`** is the model-facing function spec (`{"name", "description", "parameters"}`
  JSON Schema). The description is what the model uses to decide when to call it — the
  guide's "common mistakes" section calls vague descriptions the most frequent defect.
- **`toolset`** namespaces the registration. Re-registering a name owned by a *different*
  toolset is rejected unless `override=True` (warn + return `None`; it does not raise).
- **`handler`** returns a string — JSON for structured data. Exceptions propagate into the
  tool call, so handlers should catch and return `{"error": ...}` JSON.
- **`check_fn`** hides the tool from the model while it returns False (optional deps,
  missing credentials). `requires_env` does the same for env-gated tools.
- **`is_async=True`** registers a coroutine handler.
- **`override=True`** replaces a same-named tool. Against a built-in it additionally
  requires `plugins.entries.<plugin_id>.allow_tool_override: true` (or the
  `tools.override` capability + consent) and raises `PluginToolOverrideError` otherwise.
  The `PluginManager` records plugin tool names so unload can remove exactly what the
  plugin added; `override` is logged for audit.
- **`ctx.dispatch_tool(name, args)`** invokes any registered tool (built-in or another
  plugin's) through the normal approval/redaction/budget pipeline — the supported way for
  a slash command to act like the model.
- **`ctx.call_mcp(server, tool, arguments)`** calls an MCP tool synchronously, default
  deny: the server must be in `plugins.entries.<plugin_id>.mcp_allowlist`.

Minimal registration:

```python
def register(ctx):
    ctx.register_tool(name="calculate", toolset="calculator",
                      schema=schemas.CALCULATE, handler=tools.calculate)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
```

A registration handle is returned for every successful registration; the manager tears
them down on unload, force-reload, or `deregister()`.

Other model-facing surfaces on the same `ctx` (implemented as scoped provider
registrars or content registrars):

- `register_command(name, handler, description=...)` — in-session slash command.
- `register_cli_command(name, help, setup_fn, handler_fn=None)` — `hermes <name> ...`
  argparse subtree wired at startup without touching `main.py`.
- `register_skill(name, path)` — read-only, namespaced `<plugin>:<skill>`.
- `register_system_prompt_section(...)`, `register_middleware(kind, cb)`.
- Provider registrars: `register_memory_provider`, `register_context_engine`,
  `register_platform`, `register_telegram_handler`, `register_slack_action_handler`,
  `register_delegation_provider`, `register_computer_use_provider`,
  `register_dashboard_auth_provider`, `register_approval_transport`,
  `register_automation_blueprint`, `register_locale(_dir)`.

---

## 5. Hooks

### 5.1 Registration and calling convention

```python
ctx.register_hook(hook_name, callback)
ctx.register_hook("transform_llm_output", redact, hold_stream=True)
```

- `hook_name` must be in `VALID_HOOKS` (`hermes_cli/plugins.py`); unknown names are
  refused by the registrar and reported by `validate` / `doctor`.
- Callbacks are **keyword-inspected**: a narrow signature receives only the fields it
  declares; `**kwargs` receives the whole current payload. New fields are only ever
  added, so `**kwargs` is the forward-compatible choice.
- Callbacks run isolated: an exception is logged and skipped, the agent continues. A slow
  callback is cut off by `plugins.hook_callback_timeout` (default 30 s).
- Most hooks are fire-and-forget observers. The steering exceptions are `pre_llm_call`,
  `pre_tool_call`, `pre_gateway_dispatch`, `pre_command` (reserved/ignored), the
  `transform_*` hooks, and middleware.
- Hooks fire under the owning profile's scope. Lifecycle hooks also fire from eviction,
  gateway teardown and cron completion, so state must be keyed by the home passed in, not
  captured at import.

### 5.2 Hook families

From the authoring guide's hook table plus `VALID_HOOKS` (canonical superset):

| Family | Hooks |
|---|---|
| Turn lifecycle | `pre_llm_call`, `post_llm_call`, `pre_api_request`, `post_api_request`, `api_request_error`, `pre_auxiliary_call`, `post_auxiliary_call` |
| Tools | `pre_tool_call`, `post_tool_call`, `transform_tool_result`, `transform_terminal_output` |
| Reply shaping | `transform_llm_output`, `transform_api_error_classification`, `pre_verify` |
| Session | `on_session_start`, `on_session_end`, `on_session_finalize`, `on_session_reset`, `on_session_delete`, `post_compaction`, `agent_loop_stopped` |
| Streaming/observers | `on_stream_start`, `on_stream_delta`, `on_stream_end`, `on_interim_message`, `on_room_member_activity` |
| Approvals / human input | `pre_approval_request`, `post_approval_response`, `on_human_input_request`, `on_human_input_resolved` |
| Gateway | `pre_gateway_dispatch`, `post_gateway_admission`, `gateway_platform_event`, `pre_command` |
| Kanban | `kanban_task_claimed`, `kanban_task_completed`, `kanban_task_blocked`, `on_kanban_worker_spawned`, `on_kanban_worker_exited`, `on_kanban_worker_stale_claim`, `on_kanban_task_updated`, `on_kanban_dispatch_tick` |
| Other | `on_skill_lifecycle`, `subagent_start`, `subagent_stop`, `pre_transcription` |

Payload shapes worth memorizing:

- `pre_tool_call(tool_name, args, task_id, ...)` may return
  `{"action": "block", "message": ...}` to veto, or `{"action": "approve", "message": ...}`
  to escalate to the human approval gate.
- `post_tool_call(tool_name, args, result, task_id, duration_ms, ...)` — return ignored.
- `pre_llm_call(session_id, user_message, conversation_history, is_first_turn, model,
  platform, ...)` may return `{"context": "..."}` or a plain string to inject text into
  the **current user message** (not the system prompt, to preserve prompt caching), or
  `{"action": "block", "message": ...}` to veto the turn before any inference request.
  Per-hook injected context is capped at 10,000 chars by default; overflow spills to
  `$HERMES_HOME/hook_outputs/<session_id>/<uuid>.txt`.
- `on_session_end(session_id, completed, interrupted, model, platform, ...)` — note the
  `orca-status` manifest lists it and its `SELECTED_KEYS` only reads `session_id`.

### 5.3 Middleware (change, not just observe)

`ctx.register_middleware(kind, callback)` with `kind` ∈ `tool_request`, `llm_request`,
`tool_execution`, `llm_execution` (`VALID_MIDDLEWARE` in `hermes_cli/middleware.py`):

- Request middleware returns `{"args": {...}}` / `{"request": {...}}` to replace the
  effective payload, or `None` to leave it unchanged; `original_args` /
  `original_request` always hold the pre-middleware copy.
- Execution middleware receives `next_call` and must call it exactly once to continue.
- A raising middleware is logged and skipped; the base runtime path cannot be broken.
  Payloads carry `middleware_schema_version: hermes.middleware.v1`.

### 5.4 Plugin events (`emit` / `subscribe`)

`ctx.emit("event", payload)` publishes `<plugin_id>:event` — the namespace is forced, a
`:` in the name raises, and payloads must be dicts or None. Delivery is fire-and-forget on
a single-worker queue (order preserved, a blocking subscriber cannot stall the emitter).
`ctx.subscribe("<plugin_id>:<event>", cb)` is unrestricted and owner-tagged, so unload
removes zombie callbacks. These are the `Emits:` / `Listens:` lines in
`hermes plugins show`.

---

## 6. Capabilities and consent

Registry: `hermes_cli/plugin_capabilities.py` (`_CAPABILITY_ROWS`). Each id maps 1:1 to an
already-existing enforcement gate; ids with no enforcing surface are never minted.

| Capability | Risk shown on the consent screen |
|---|---|
| `tools.override` | Replace built-in tools (e.g. `shell_exec`, `write_file`) — an override can intercept everything routed through that tool |
| `llm.provider_override` | Run host-owned LLM calls against a different provider (uses your credentials) |
| `llm.model_override` | Choose the model for host-owned LLM calls (spend follows the model) |
| `llm.agent_id_override` | Attribute its LLM calls to a different agent id |
| `llm.profile_override` | Run LLM calls under a different auth profile |
| `llm.task_override` | Route LLM calls through the host's auxiliary task lanes |
| `llm.side_question` | Ask your agent a short question after each turn (sees the session, spends main-model tokens) |
| `gateway.platform_actions` | Act on connected chat platforms as the bot (reactions, thread titles) |

Model:

- Declared in the manifest: `capabilities: [tools.override, llm.model_override]`.
- **Consent once, at install/enable.** A later version that adds a capability asks again
  for just the addition; a catalog re-pin that adds one also asks before updating.
- **Fail closed.** Undeclared or unconsented capabilities are simply off. Probe and
  degrade:

  ```python
  def register(ctx):
      if ctx.has_capability("tools.override"):
          ctx.register_tool(..., override=True)
      else:
          ctx.register_tool(...)   # non-conflicting name
  ```

- Consent state is stored on `plugins.entries.<id>.granted_capabilities` /
  `capabilities_consent`; the older per-capability booleans
  (`allow_tool_override`, `llm.allow_model_override`, `allow_platform_actions`, …) still
  work but are deprecated. Unknown capability ids are dropped with a warning.
- Pip-distributed plugins declare capabilities through the
  `hermes_agent.plugin_capabilities` entry-point group (`<plugin-id>.<capability-id>`),
  so consent/audit works without importing the code.
- **Not a sandbox.** Capabilities gate host API surfaces only. In-process plugin code can
  read any file the user can; `hermes plugins doctor` blocks direct socket connections
  during registration but explicitly says it is not a sandbox.

Observed on this install:

```console
$ hermes plugins capabilities
No plugins declare or hold capabilities.
```

---

## 7. Agent Plugins v1 packaging

Hermes also installs directory packages that target the portable **Agent Plugins v1.0.0**
format. It is a compatibility adapter for the portable components Hermes already owns —
not a replacement for native `plugin.yaml` + `register(ctx)`.

### 7.1 Layout

```text
my-portable-plugin/
├── plugin.json
├── skills/
│   └── summarize/
│       ├── SKILL.md
│       └── references/
└── mcp.json
```

Real example installed here — `~/.hermes/plugins/hermes-dreaming/plugin.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "hermes-dreaming",
  "version": "2.7.0",
  "description": "Nightly deterministic memory consolidation for Hermes: ...",
  "author": { "name": "Anton Vaskov", "url": "https://t.me/passone" },
  "homepage": "https://github.com/itpartypattaya/hermes-dreaming",
  "repository": "https://github.com/itpartypattaya/hermes-dreaming",
  "license": "MIT",
  "keywords": ["hermes", "hermes-agent", "memory", "consolidation", "dreaming", "cron", "skill"]
}
```

Its tree is `plugin.json` + `skills/dreaming/` (SKILL.md, examples, references, scripts) and
no Python `__init__.py` — portable packages do not import Python.

### 7.2 Semantics

- Installed **disabled**: `hermes plugins install owner/repo --no-enable` then
  `hermes plugins enable <name>`. Enabling is the trust decision.
- Enabled package components: `skills/*/SKILL.md` (read-only, namespaced
  `<plugin-name>:<skill>`; first-loaded wins on collisions) and stdio / Streamable HTTP
  MCP servers from root `mcp.json`. Legacy `sse` entries are reported and skipped.
- MCP commands are one executable token plus an argument list — never a shell string.
- MCP server names are global like a user's own `mcp_servers`; a `config.yaml` server wins
  over a package server, and the first-loaded package wins over the next (loser skipped
  with a warning).
- Env: package `env` values are **visible package data, not secret storage**. To forward
  a user variable, write `"NAME": "${NAME}"`; unset/empty means the line is ignored.
- `PLUGIN_ROOT` = resolved package root. `PLUGIN_DATA` = profile-scoped writable dir
  managed by Hermes (`plugin-data/agent-plugin-<slug>-<hash>`).
- Validated locally: `plugin.json`, Agent Skills frontmatter, fixed component locations,
  `mcp.json`, resolved paths, symlink containment. Schemas are not fetched at load time.
  A bad skill or MCP entry is skipped at its boundary; valid siblings still load.
- **Trust model:** Agent Plugins v1 defines no trust, permissions, provenance or sandbox.
  Enabling a package grants its instructions and local executable the same full-trust
  posture as any installed Hermes plugin.
- `plugin.json` has no `requires_hermes`; the catalog entry's floor is the only one.

### 7.3 Hermes vendor extension

Agent Plugins v1 rejects unknown top-level fields, so Hermes-specific declarations live
under `extensions."com.nousresearch.hermes"`:

```json
{
  "extensions": {
    "com.nousresearch.hermes": {
      "config_schema": {
        "api_key": { "type": "secret", "env": "MY_PLUGIN_API_KEY" },
        "show_chip": { "type": "bool", "default": true, "label": "Show the status-bar chip" }
      },
      "config_keys": ["memory.retaindb"],
      "servers": {
        "trade": { "trust": "untrusted" },
        "renders": {
          "trust": "untrusted",
          "costs": { "create_image": 20, "animate": { "credits": [5, 20], "note": "per animation" } },
          "spend_limit": 100,
          "cost_unit": "credits",
          "timeout": 1000
        }
      }
    }
  }
}
```

- `servers.<name>.trust` ∈ `full` (default) | `untrusted`; only narrowing is allowed. With
  `untrusted`, non-`readOnlyHint` calls ask Once/Session/Always and fail closed where
  nobody can answer (cron, unattended runs).
- `costs` keys are MCP tool names; values are a number, `[min, max]`, or
  `{credits, note}`. `spend_limit` (0/absent = none) and `cost_unit` (default `credits`)
  map to native `tool_costs` / `spend_limit` / `cost_unit`. Costs survive a same-name
  `config.yaml` server entry; the user's `spend_limit` wins, including `0`.
- `timeout` is a per-server MCP tool-call budget in seconds; it extends the wait budget,
  not the permission/cancel policy.
- Unknown server keys warn and load (for forward compatibility), so a new key should ship
  with a catalog `requires_hermes` floor.

Status: the rendered spec calls v1.0.0 a Working Draft while the versioned repo records
it as Published; Hermes keys behavior on the canonical v1.0.0 schema identifiers, and
this is an explicitly supported **subset**, not full conformance.

---

## 8. Installing from a Git repo

From `hermes plugins --help` and `hermes plugins install --help`:

```text
usage: hermes plugins install [-h] [--force] [--ref COMMIT_SHA]
                              [--allow-removed] [--no-deps | --yes-deps]
                              [--enable | --no-enable]
                              identifier

identifier   Bare plugin catalog entry name, Git URL, or owner/repo shorthand
             (e.g. anpicasso/hermes-plugin-chrome-profiles)
--ref         Install exactly one immutable 40-character Git commit SHA
--force       Reinstall over an existing plugin (from the same source, untracked files kept)
--allow-removed  DANGEROUS: bypass the catalog removed-plugin blocklist
--no-deps     Download without dependency consent and leave disabled
--yes-deps    Answer the Python dependency consent question yourself (headless installs)
--enable / --no-enable  Skip the "Enable now? [y/N]" prompt
```

Typical flows:

```bash
# catalog entry at its reviewed pin
hermes plugins install <name>

# arbitrary public repo
hermes plugins install owner/repo
hermes plugins install owner/repo --enable
hermes plugins install owner/repo --no-enable

# reproducible / private
hermes plugins install owner/repo --ref 0123456789abcdef0123456789abcdef01234567
hermes plugins install git@host:owner/repo.git
hermes plugins install owner/repo/path/to/subdir     # subdirectory install

hermes plugins list
hermes plugins enable <name>
hermes plugins disable <name>
hermes plugins check-updates
hermes plugins update <name>
hermes plugins remove <name>
```

Behavior details:

- **Subdirectory installs** (`owner/repo/path/to/plugin`) download only that folder; they
  do not carry a local Git checkout.
- **Private repos** clone non-interactively. Credential order: `GITHUB_TOKEN` /
  `GH_TOKEN` from `.env` (GitHub hosts only) → `gh auth login` (GitHub only) → the git
  credential helper for that host. The credential is sent as a one-shot HTTP header and
  is never written to the plugin's `.git/config` or install metadata. `git@host:` sources
  use ssh-agent.
- **Installed ≠ enabled.** Install prompts `Enable 'name' now? [y/N]`, default no. A
  memory provider asks `Use 'name' as the memory provider now?` and sets
  `memory.provider` on yes.
- **Provenance** is written by the installer to `~/.hermes/plugins/.install-metadata.json`
  (canonical source, installed revision, pin, catalog block). A repo can ship a forged
  `.hermes-catalog.json`, but Python never trusts it.
- `--force --ref <new>` replaces code from the same source while keeping untracked and
  git-ignored files; the previous tree is backed up to
  `~/.hermes/plugins-backup/<name>-<sha>/`.
- A self-cloned directory is untracked until `hermes plugins adopt <name>` records its Git
  origin. `hermes plugins update` moves tracked Git installs and re-pins catalog installs;
  pinned installs stay pinned. Auto-update (`plugins.auto_apply`) applies the same rules.
- The catalog's `removed.yaml` blocklist is checked at install, update, enable and load;
  matching is by canonical `host/owner/repo`, so `git@` / `ssh://` / `www.` spellings all
  match. Only `--allow-removed` (recorded on the install) exempts a plugin, and only from
  the CLI.
- Catalog admission runs `hermes plugins validate <path> --install-deps` (the same CI
  gate) at a mandatory 40-hex pin.

---

## 9. Running background work / a scheduler from a plugin

There is no single "start a scheduler" method. Four supported shapes, in increasing
weight:

### 9.1 `ctx.spawn_task(coro)` — supervised asyncio task

`hermes_cli/plugins.py`:

> "Spawn a supervised asyncio task; unload/force reload cancels it. Needs a running loop."

```python
async def _tick_forever():
    while True:
        await asyncio.sleep(30)
        ...

def register(ctx):
    ctx.spawn_task(_tick_forever(), name="calendar-reminders")
```

- Requires a running event loop (`asyncio.get_running_loop()`), so it is usable from a
  gateway/CLI process but not from a plain import.
- The task is tracked as a `background_task` registration and cancelled on unload.
- For memory/context providers the documented pattern is
  `agent.memory_provider.spawn_context_thread`, not a bare `threading.Thread` — a bare
  thread runs with no profile scope and fails closed (or writes into the launch
  profile's tenant).

### 9.2 Cron scheduler provider plugin

A plugin whose `__init__.py` mentions `register_cron_scheduler` or `CronScheduler` is
discovered as a **cron scheduler provider** (`plugins/cron_providers/__init__.py`):

- Search: bundled `plugins/cron_providers/<name>/`, then
  `$HERMES_HOME/plugins/<name>/` (bundled wins on collision). Discovery is a cheap text
  heuristic, so a user plugin dropped in `~/.hermes/plugins/` is eligible.
- Selected by `cron.provider` in `config.yaml`; empty means the built-in
  `InProcessCronScheduler` (core, not discoverable).
- The provider implements `cron.scheduler_provider.CronScheduler` and calls
  `register_cron_scheduler(provider)` or exposes a subclass/instance.
- Scope is deliberately narrow: *"Providers decide only when a job fires — execution +
  delivery stay in cron.scheduler.run_job / _deliver_result; never reimplement them."*
  The ABC is marked **EXPERIMENTAL** and growth must be additive.
- Example: bundled `chronos` ("NAS-mediated managed cron provider for scale-to-zero
  hosted agents… Inert unless cron.provider=chronos").

This is the right surface if you want to change **when** jobs fire (external trigger,
hosted scheduler, scale-to-zero), not to add a private timer for one plugin.

### 9.3 Ordinary cron jobs + automation blueprints

- The agent-facing `cronjob` tool and `hermes cron add/list/run/edit/remove` create
  normal jobs. Jobs have a schedule, prompt, optional skills, and a `deliver:` target;
  they run through the gateway ticker or the active cron provider.
- `ctx.register_automation_blueprint(key, *, title, description, schedule_template,
  prompt_template, category="general", slots=(), deliver_default="origin", skills=(),
  tags=())` adds a fill-in-the-blanks template to `/blueprint`, the dashboard Blueprints
  tab and the Desktop Cron page, namespaced `<plugin>:<key>`. Invalid templates warn and
  skip; the rest of the plugin still loads.
- **Jobs outlive the plugin.** Scheduling a blueprint creates an ordinary cron job;
  disabling/uninstalling the plugin removes the blueprint but leaves the jobs running.
  A job referencing a skill the disabled plugin bundled skips that skill and logs it.
- Internal (not documented public API): `cron.jobs.create_job(**kwargs)` /
  `update_job()` and `cron.scheduler.create_job_with_scheduler_registration(**kwargs)`
  (persist + register the first trigger with the active provider). A plugin could call
  these, but they are core internals — the supported path is asking the agent to use the
  `cronjob` tool or registering a blueprint.

### 9.4 External scheduler + hook entry point

A plugin that only needs to *react* can expose a tool and let an external scheduler
(cron, systemd timer, Orca job) call `hermes`/the tool. Conversely a shell hook
(`hooks:` in `config.yaml`) runs a command on plugin events with no Python at all:
`hermes` shell hooks support the same events as Python hooks plus
`pre_gateway_dispatch`, and are refused for `transform_api_error_classification`.

---

## 10. Sending notifications

Notification surfaces, from innermost to widest:

1. **`ctx.post_message(text)` / `await ctx.apost_message(text)`** — one message in the
   conversation bound to the current call (a tool/hook running in its turn, its slash
   command). Returns a handle with `.ok`, `.message_id`, `.error` and `.edit(text)`.
   Routing is `gateway/plugin_messages.py`. The plugin never names a chat, so it cannot
   reach another one. **Outside any session (a timer thread, cron, no live gateway) the
   call is refused with a result, never an exception** — a background scheduler must not
   assume this works from a bare thread.
2. **`ctx.inject_message(content, role="user", *, session_key=None, origin=None)`** —
   queues content for a session or a chat in the plugin's own profile. `True` means a host
   accepted it. Requires `plugins.entries.<id>.allow_gateway_injection: true` (default
   off; read from the plugin's own profile config, so it fails closed). `session_key` and
   `origin` are mutually exclusive; the "keyless" form needs a live gateway. This is the
   supported way to wake a chat/session from background work.
3. **`ctx.platform_actions`** — capability-gated facade for gateway-bot verbs
   (`add_reaction`, `set_thread_title`). Every call re-checks
   `gateway.platform_actions` (legacy `allow_platform_actions`), default OFF, and returns
   `{"ok": bool, ...}` instead of raising. No adapter handles or raw SDK objects cross.
4. **Cron `deliver:` targets** — a scheduled job's final output is delivered by cron
   itself (origin chat, `telegram:<id>`, `homeassistant`, `bot-chat`, `all`, combinations
   like `origin,all`). Delivery is secret-redacted, wrapped with a header/footer unless
   `cron.wrap_response: false`, and sent with the platform notification flag by default
   (`cron.delivery.notify`). A plugin that ships a platform adapter can opt in to cron
   delivery with `cron_deliver_env_var` (see `plugins/platforms/*/plugin.yaml`).
5. **Approval transport plugin** — `ctx.register_approval_transport(name, present_fn)`
   registers a transport that is inactive until `security.approval.transport: <name>`
   selects it. It receives a redacted `ApprovalRequest` and returns only a correlated
   decision; policy and persistence stay host-owned. Real example: `ntfy-approvals`
   sends push messages with Allow-once / Deny buttons.
6. **Full platform adapter** (`kind: platform`, `ctx.register_platform(...)`) — a channel
   integration with its own send path, home-channel env var and cron delivery. Example:
   `homeassistant` delivers replies as HA persistent notifications and routes
   `deliver=homeassistant` through `notify.notify`.
7. **Shell hook** — no Python: `hooks: [{event: post_tool_call, command: "notify-send
   'Tool ran: {tool_name}'", when: {tools: [terminal]}}]` for OS-level desktop alerts.
8. **Gateway event hooks** — drop `HOOK.yaml` + `handler.py` in `~/.hermes/hooks/<name>/`;
   events include `gateway:startup`, `session:start`, `session:end`, `agent:start`,
   `agent:step`, `agent:end`, `command:*`. Placing the files *is* the opt-in (no
   `plugins.enabled` step); errors are logged and never block the pipeline.

---

## 11. Validation / doctor workflow (observed)

`hermes plugins validate --help`:

```text
usage: hermes plugins validate [-h] [--install-deps] [--json] path

positional arguments:
  path            Path to the plugin directory

options:
  -h, --help      show this help message and exit
  --install-deps  Install the plugin's declared Python dependencies
                  (pyproject/python_dependencies) into this venv before the
                  capability probe, exactly as `plugins install` would —
                  the catalog CI gate
  --json          Print machine-readable JSON (for CI)
```

Real run against a copy of `orca-status` (copied to the Hermes host because the CLI runs
over SSH):

```console
$ hermes plugins validate /tmp/orca-status-validate/orca-status
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — not declared
✓ config schema — not declared
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no tools to check
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
⚠ private core imports (catalog rule 9: public surfaces only; these can break on
any release): __init__.py:11 agent.session_client_env.bound_client_env. Use 
ctx.* / documented plugin APIs, or open an issue for the seam you need.
◆ Isolation — runs in the plugin host (plugins.isolation: host)

Validation passed.
```

Two takeaways:

- The "declared hooks — matches registrations" check is what makes `provides_hooks`
  meaningful; drift blocks catalog admission even though it does not block loading.
- Even a passing plugin can carry warnings (here: a private core import), and isolation
  mode is reported per plugin.

`hermes plugins doctor [path|id] --ci` runs the same discovery, manifest parser,
namespaced import, `register(ctx)`, hook registry and tool registry with a temporary
`HERMES_HOME`, restores registration state afterwards, and blocks direct Python socket
connections during registration. It is **not** a sandbox: plugin code still runs
in-process with the user's permissions and can spawn subprocesses.

---

## 12. Implications for a Hermes calendar plugin

Not part of the required scope, but the direct consequences of the above:

- **Manifest:** `name`, `version`, `description`, `provides_tools`, `provides_hooks`,
  `requires_env` for any API key, `config_schema` for user-visible settings, and
  `capabilities` only if it overrides a built-in or picks an LLM model. Add
  `manifest_version: 2` if `config_schema` / `requires_plugins` are used.
- **Tools:** register calendar CRUD as tools with precise schemas; return JSON strings;
  use `check_fn` for credential-gated tools; never shadow a built-in without both
  `override=True` and the `tools.override` capability + consent.
- **Reminders:** a native `ctx.spawn_task` loop is fine only where a live event loop
  exists and state is profile-keyed; durable reminders should be ordinary cron jobs
  (or an automation blueprint) so they survive restarts, and delivery should use a
  cron `deliver:` target rather than a plugin thread posting into a conversation.
- **Notifications:** `ctx.post_message` is turn-scoped only; background delivery needs
  `ctx.inject_message` plus `allow_gateway_injection`, or cron delivery.
- **State:** `ctx.state` (≤10 MiB, profile-scoped) or `plugin_data_dir()` /
  `plugin_db()` — never the install directory, which `update`/`remove` replaces.
- **Validation:** wire `hermes plugins validate --json` and `hermes plugins doctor --ci`
  into CI; avoid private core imports (the one warning on `orca-status`).

---

## 13. Open questions / caveats

- The cron provider ABC is explicitly EXPERIMENTAL until a second provider validates the
  shape; treat it as an unstable extension point.
- `cron.jobs.create_job` / `cron.scheduler.create_job_with_scheduler_registration` work
  but are **internal**; only `ctx.register_automation_blueprint` and the `cronjob` tool
  are documented plugin-facing scheduling surfaces.
- `hermes plugins capabilities` reported no declaring plugins on this install, so the
  consent screen itself was not observed end-to-end here; the model above is from
  `plugin_capabilities.py` and the authoring guide.
- Capability consent is not a security boundary. The only real isolation is
  `plugins.isolation: host`, and even that is a per-profile process boundary, not a
  sandbox.
- Several surfaces differ per Hermes version (hook names grow additively, manifest v2 is
  recent). Pin a `requires_hermes` floor for anything that depends on a newer key.
