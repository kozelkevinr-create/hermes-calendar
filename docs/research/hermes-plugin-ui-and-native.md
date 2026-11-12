# Hermes Plugin UI and Native Code — Research Notes

Follow-up to [`hermes-plugin-api.md`](./hermes-plugin-api.md). Covers two owner
decisions:

1. The calendar plugin will be **published for others to install**.
2. Its **UI runs on the Hermes host**, not on the user's own machine.

Everything here was gathered read-only from the running Hermes 0.23.0 install on
`docker-host` (`ssh docker-host`). No files or services were changed.

---

## 0. Conclusions up front

**UI.** A plugin adds UI through the **web dashboard**, not through a Python API on
`ctx`. A plugin directory may contain `dashboard/manifest.json` + a pre-built JS
bundle + an optional `dashboard/plugin_api.py` FastAPI router. The router is mounted at
`/api/plugins/<name>/`, the JS is served from `/dashboard-plugins/<name>/`, and the router
sits behind the dashboard's normal auth gate (the static JS/CSS are served without auth on
purpose — `<script>` tags cannot attach tokens — so they are enabled-gated and restricted
to a suffix allowlist; see §7). This fits the owner decision: the UI is served
by the dashboard process on the host, so any browser on the LAN/VPN sees it. The native
**Desktop app's** plugin SDK is the wrong layer here — its JS runs in the Desktop app on
the user's machine, even when the app talks to a remote backend.

**Native code.** Hermes has no Rust-specific support. What it supports:

| Shape | Mechanically supported? | Catalog-legal? | Host needs |
|---|---|---|---|
| PyO3 extension **wheel on PyPI**, declared as a normal dependency | Yes — PM/uv installs it | Yes (the binary is not in the plugin tree) | Nothing if a matching wheel exists; Rust+C toolchain if uv has to build an sdist |
| Maturin plugin **built from source by uv** at install/enable | Yes — `[build-system]` makes the dir a workspace member | Yes (source, not binary) | `cargo`/`rustc` (absent on this host) + a C linker; rebuild per enable/update |
| Committed/prebuilt **binary in the plugin tree** | Mechanically yes (Python `subprocess`) | **No** — catalog rule 17 | — |
| **Sidecar binary** built/fetched at setup into `PLUGIN_DATA` | Yes | Yes, if built by the repo's public CI from source with attestation, or fetched from the upstream tool's official source with a pinned checksum and disclosure | Matching OS/arch/libc; a setup step the plugin runs |
| **MCP server** (stdio binary or streamable-http) | Yes, for portable packages via `mcp.json`; for native plugins via the user's `config.yaml` | Yes, same binary rules | The binary (as above), or network reachability for HTTP |

There is **no install-time script hook**. `hermes plugins install` clones, optionally
installs Python dependencies, shows `after-install.md`, and stops. Anything that must
build or download a binary has to happen in `register()`, a plugin CLI command, or the
first tool call. Putting it in `register()` is a poor choice: `hermes plugins validate`
imports the plugin and calls `register(ctx)` in a subprocess probe with a throwaway
`HERMES_HOME` (so a download would run during catalog CI), and `hermes plugins doctor`
blocks direct socket connections during that probe.

---

## 1. Sources

Files read on `docker-host`:

| Path | Used for |
|---|---|
| `~/.hermes/hermes-agent/website/docs/user-guide/features/extending-the-dashboard.md` | Canonical dashboard-plugin reference (945 lines) |
| `~/.hermes/hermes-agent/website/docs/user-guide/features/web-dashboard.md` | Auth gate, providers, remote access, REST API |
| `~/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md` | Desktop SDK (contrast) |
| `~/.hermes/hermes-agent/hermes_cli/web_server_dashboard.py` | Plugin discovery, API mount gates, hot mount |
| `~/.hermes/hermes-agent/hermes_cli/web_routers/dashboard_ui.py` | `/dashboard-plugins/` asset serving, suffix allowlist |
| `~/.hermes/hermes-agent/hermes_cli/web_server.py` | Middleware order, auth gate, plugin runtime gate |
| `~/.hermes/hermes-agent/hermes_cli/dashboard_auth/*.py` | Auth middleware, cookie/session, native tickets |
| `~/.hermes/hermes-agent/hermes_cli/plugins_cmd_inspect.py` | Agent/desktop component detection |
| `~/.hermes/hermes-agent/hermes_cli/plugin_validate.py` | Catalog validation checks |
| `~/.hermes/hermes-agent/pm/plugin_declarations.py` | Dependency declaration parsing, `external`, URL rejection |
| `~/.hermes/hermes-agent/pm/build_compilers.py`, `pm/native_build.py` | Native build toolchain handling |
| `~/.hermes/hermes-agent/pm/workspace.py`, `pm/publication.py`, `pm/install.py` | Plugin-as-workspace-member admission |
| `~/.hermes/hermes-agent/hermes_cli/agent_plugins.py` | Portable `mcp.json` parsing, path scoping |
| `~/.hermes/hermes-agent/tools/plugin_guard.py`, `tools/skills_guard.py` | Install scanner, binary findings |
| `~/.hermes/hermes-agent/website/docs/developer-guide/plugins/catalog-submission.md` | Catalog rule 17 (native payloads) |
| `~/.hermes/hermes-agent/plugins/kanban/dashboard/manifest.json` + `plugin_api.py` | Real bundled dashboard plugin |
| `~/.hermes/plugins/hermes-dreaming/plugin.json` + `skills/` | Real portable package (no Python, no UI) |
| `/etc/systemd/system/hermes-dashboard.service` + drop-ins | How this host actually runs the dashboard |
| `~/handbook/hermes-operations.md` (lines 27, 80-99) | Operator notes for this dashboard unit |

Commands run:

```bash
hermes dashboard --help
hermes dashboard --status
hermes config get dashboard
hermes plugins show kanban            # not installed as a user plugin
hermes plugins install --help
hermes plugins validate --help
curl -s http://<dashboard-host>:9119/api/status
python3 -V; command -v cargo rustc cc gcc clang maturin uv
grep -o '^HERMES_DASHBOARD[A-Z_]*' ~/.hermes/.env ~/.hermes/dashboard-auth.env
systemctl cat hermes-dashboard
```

---

# Part 1 — UI

## 2. The three dashboard extension layers

`extending-the-dashboard.md` opens with exactly three:

1. **Themes** — YAML in `~/.hermes/dashboard-themes/`, repaint the palette/typography/
   layout. No code. This host has `phosphor.yaml`.
2. **UI plugins** — a directory with `manifest.json` + a pre-built JS bundle; registers a
   tab, replaces a built-in page, augments one via page-scoped slots, or injects into
   named shell slots.
3. **Backend plugins** — a Python file (`plugin_api.py`) in the plugin directory that
   exports a FastAPI `router`; mounted at `/api/plugins/<name>/`.

All three are drop-in at runtime: no repo clone, no `npm run build`, no patching the
dashboard.

## 3. Where the UI lives inside a plugin

```
~/.hermes/plugins/my-plugin/
├── plugin.yaml              # optional — agent-side manifest
├── __init__.py              # optional — agent hooks/tools
└── dashboard/               # dashboard extension
    ├── manifest.json        # required — tab config, icon, entry point
    ├── dist/
    │   ├── index.js         # required — pre-built IIFE bundle
    │   └── style.css        # optional — injected as <link>
    └── plugin_api.py        # optional — FastAPI routes
```

The same folder can carry the agent half, the dashboard half, and the desktop half
(`desktop/plugin.js`); none is required.

Manifest (`dashboard/manifest.json`):

```json
{
  "name": "my-plugin",
  "label": "My Plugin",
  "description": "What this plugin does",
  "icon": "Sparkles",
  "version": "1.0.0",
  "tab": {
    "path": "/my-plugin",
    "position": "after:skills",
    "override": "/",
    "hidden": false
  },
  "slots": ["sidebar", "header-left"],
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Identifier used in URLs and registration — **must match the plugin's registry key** (see §9) |
| `label` | yes | Nav label |
| `icon` | no | Lucide name; unknown → `Puzzle` |
| `version` | no | Semver; default `0.0.0` |
| `tab.path` | yes | Route path (defaults to `/<name>`) |
| `tab.position` | no | `end` (default), `after:<segment>`, `before:<segment>` |
| `tab.override` | no | Replace a built-in page (`/`, `/sessions`, `/config`, …) |
| `tab.hidden` | no | Register component/slots without a nav tab |
| `slots` | no | Documentation only; real registration is `registerSlot()` in JS |
| `entry` | yes | JS bundle path relative to `dashboard/` |
| `css` | no | CSS path injected as `<link>` |
| `api` | no | Python router file, mounted at `/api/plugins/<name>/` |

A real bundled example is `plugins/kanban/dashboard/manifest.json`: tab at `/kanban`,
`position: "after:skills"`, `entry: "dist/index.js"`, `css: "dist/style.css"`,
`api: "plugin_api.py"`.

## 4. The plugin SDK and registration flow

Plugins do not bundle React. `window.__HERMES_PLUGIN_SDK__` exposes:

- `SDK.React`, `SDK.hooks.{useState,useEffect,useCallback,useMemo,useRef,useContext,createContext}`
- `SDK.components.*` — shadcn primitives (`Card`, `Button`, `Input`, `Badge`, `Tabs`,
  `Select`, `Separator`, `PluginSlot`, …)
- `SDK.api` — typed client (`getStatus`, `getSessions`, `getConfig`, …)
- `SDK.fetchJSON` — fetch wrapper that injects the session auth token, throws on error,
  parses JSON
- `SDK.utils.{cn,timeAgo,isoTimeAgo}`, `SDK.useI18n`

Bundle shape is a single IIFE (any bundler with React external):

```javascript
(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { Card, CardHeader, CardTitle, CardContent } = SDK.components;

  function CalendarPage() {
    return React.createElement(Card, null,
      React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Calendar")),
      React.createElement(CardContent, null,
        React.createElement("p", { className: "text-sm text-muted-foreground" }, "Upcoming events")));
  }

  window.__HERMES_PLUGINS__.register("my-plugin", CalendarPage);
})();
```

Load lifecycle (`extending-the-dashboard.md` → *Plugin load lifecycle*):

1. `main.tsx` exposes the SDK and registry.
2. `App.tsx` calls `usePlugins()` → `GET /api/dashboard/plugins`.
3. For each manifest: CSS `<link>`, then `<script>` for the bundle.
4. The IIFE calls `register(name, Component)` and optionally
   `registerSlot(plugin, slot, Component)`.
5. The dashboard mounts the component as a route unless `hidden`.

A plugin has **2 seconds** after script load to call `register()`; later registration
still appears because the nav is reactive. A failed script logs a console warning and is
skipped.

Discovery scans three roots (first name wins):
`~/.hermes/plugins/<name>/dashboard/` → bundled `<repo>/plugins/[memory/]<name>/dashboard/`
→ `./.hermes/plugins/...` only when `HERMES_ENABLE_PROJECT_PLUGINS` is truthy.

Force a rescan without restart:

```bash
curl http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

## 5. Slots and page replacement

Shell-wide slots: `backdrop`, `header-left`, `header-right`, `header-banner`, `sidebar`
(only when `layoutVariant === "cockpit"`), `pre-main`, `post-main`, `footer-left`,
`footer-right`, `overlay`.

Page-scoped slots: `<page>:top` / `<page>:bottom` for `sessions`, `analytics`, `logs`,
`cron`, `skills`, `config`, `env`, `docs`, `chat`.

```javascript
window.__HERMES_PLUGINS__.registerSlot("my-plugin", "cron:top", CalendarSummaryCard);
```

`tab.override: "/"` replaces a built-in page wholesale; `tab.hidden: true` plus a
page-scoped slot augments one without adding a tab. `SDK.components.PluginSlot` lets a
plugin expose nested slots of its own.

For the calendar plugin the natural shape is a real tab (`/calendar`) plus an optional
`cron:top` or `pre-main` card for the next event.

## 6. Backend routes (`plugin_api.py`)

```python
# ~/.hermes/plugins/my-plugin/dashboard/plugin_api.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/events")
async def events():
    return {"items": []}

@router.post("/events")
async def create_event(body: dict):
    return {"ok": True, "received": body}
```

Mount rules from `hermes_cli/web_server_dashboard.py`:

- The `api` value must be a **relative file inside `dashboard/`**; an absolute path or
  `../` is refused (`_safe_plugin_api_relpath`, GHSA-5qr3-c538-wm9j), and the plugin still
  loads its static JS/CSS.
- The file must export `router` (a FastAPI `APIRouter`) and is mounted at
  `/api/plugins/<name>/` (`_mount_plugin_api_routes`).
- **Trust gates** (`_plugin_api_mount_skip_reason`): user plugins must be in
  `plugins.enabled` and not in `plugins.disabled` before their Python is imported; bundled
  plugins respect an explicit disable; **project plugins are never auto-imported** because
  they ship with the CWD (GHSA-5qr3-c538-wm9j). The same gate runs per request
  (`_plugin_api_request_refused`) because a plugin disabled at runtime keeps its router
  mounted until restart.
- With `plugins.isolation: host`, the router runs in the profile's plugin host process and
  the dashboard forwards each request over IPC (`_mount_hosted_plugin_api`,
  `hermes_cli/plugin_host_http_auth.py`); a slow handler gets HTTP 504 after 60 s instead
  of blocking the dashboard loop.
- Routes are hot-mounted on first request after install/enable (`_PluginApiHotMount`), so
  no restart is needed, provided the plugin is enabled.
- **Do not block the event loop**: `plugins.isolation: in_process` means handlers share the
  loop with `/api/status`, chat, and Desktop. Use `async def` + async I/O, or
  `await asyncio.to_thread(...)`; a plain `def` handler also runs in a worker thread.
- Handlers may import Hermes internals (`hermes_state.SessionDB`,
  `hermes_cli.config.load_config`) because they run in the dashboard process — but that is
  not part of the plugin compatibility contract.

Auth of these routes: they sit behind the dashboard's normal gate. Unauthenticated
requests get `401` before the plugin route runs; `SDK.fetchJSON` sends the session token.

## 7. Static assets and their unusual auth

`hermes_cli/web_routers/dashboard_ui.py`:

```python
@router.get("/dashboard-plugins/{plugin_name}/{file_path:path}")
async def serve_plugin_asset(...):
    """Serve static assets from a dashboard plugin's ``dashboard/`` directory.

    Unauthenticated on purpose: the SPA loads plugin JS via <script src> and CSS via
    <link href>, which cannot attach an auth header. Hence the suffix allowlist ...
    user plugins must be enabled ..."""
```

Consequences:

- Assets are served **without authentication** so `<script>`/`<link>` work; the
  compensating controls are (a) an allowlist of suffixes (`.js`, `.mjs`, `.css`, `.json`,
  `.html`, `.svg`, images, fonts, `.map`) and (b) the plugin must be enabled. A `.py`,
  `.env.example`, or README is 404'd, so `plugin_api.py` source is not downloadable.
- Path traversal is blocked with `resolve().is_relative_to(base)`.
- The allowlist means a dashboard plugin cannot serve a `.wasm` or extensionless binary to
  the browser through this route. Keep the UI as JS/CSS; do the native work server-side.

## 8. Dashboard auth, and reaching it from another machine

### 8.1 When the gate engages

`web-dashboard.md` → *When the gate engages*: the auth gate is on **iff** the bind host is
not `127.0.0.1`, `::1`, or `localhost`. `--insecure` is a deprecated no-op and does not
disable it. If the gate would engage and no provider is registered, the dashboard
**refuses to bind** (fail closed; the docs show the exact error text, and exit 78 is a
different refusal: "this host is already served by PID … on another port").

### 8.2 Providers

| Provider | Enables when | Suited for |
|---|---|---|
| `basic` (username/password) | `dashboard.basic_auth.username` + `password_hash` (or `password`), or the `HERMES_DASHBOARD_BASIC_AUTH_*` env vars | Trusted LAN / VPN only; scrypt-hashed, HMAC-signed stateless sessions, rate-limited login |
| `nous` (Nous Portal OAuth) | `HERMES_DASHBOARD_OAUTH_CLIENT_ID` or `dashboard.oauth.client_id`; registered with `hermes dashboard register` | Public internet |
| self-hosted OIDC | `dashboard.oauth.self_hosted.*` | Public internet behind your IdP |
| custom | `ctx.register_dashboard_auth_provider(...)` in a plugin | Anything else |
| `drain` (bearer) | `HERMES_DASHBOARD_DRAIN_SECRET` | Service-to-service |

Multiple providers stack; `/login` lists them. Every login start/success/failure is
appended to `$HERMES_HOME/logs/dashboard-auth.log` with tokens/headers redacted.

The auth middleware attaches the verified `Session` to `request.state.session`
(`hermes_cli/dashboard_auth/middleware.py`). Sensitive core routes additionally require
the SPA token via `_require_token` (`hermes_cli/web_server.py`), which defers to the gate
in gated mode. **Plugin routes get the gate, not a per-user identity** — there is no
plugin-facing "current user" object; treat any logged-in session as the operator.

### 8.3 Reaching the host

Three deployment shapes, from the docs:

1. **Loopback + tunnel** (most conservative): `hermes dashboard --host 127.0.0.1 --port 9119`
   and reach it over an SSH tunnel or Tailscale. No auth gate, because only loopback can
   connect.
2. **LAN/VPN bind + basic auth**: `--host 0.0.0.0` (or a specific LAN IP) with
   `dashboard.basic_auth.username` + `password_hash` + `secret`. The docs explicitly warn
   this is not for public-internet exposure.
3. **Reverse proxy / public URL**: bind loopback, set `dashboard.public_url` to the
   external origin and list the proxy in `dashboard.trusted_proxies`; a non-loopback
   `public_url` always engages the gate. Tailscale Serve with an HTTPS
   `https://<machine>.<tailnet>.ts.net` origin is the documented example.

One backend serves a whole host: a second `hermes dashboard` that cannot serve the typed
`--host`/`--port` refuses with exit 78 and names the owner. Use `--isolated` for a
profile-scoped server.

Verify from another machine:

```bash
curl -s http://HOST:9119/api/status | jq '.auth_required, .auth_providers'
# true
# ["basic"]
```

### 8.4 Example deployment

A typical self-hosted deployment runs `hermes dashboard --host <lan-address> --port 9119 --no-open`
under a service manager, with the sign-in secret kept in a mode-600 environment file outside the
plugin tree. Host-specific unit files and addresses are intentionally omitted from this document.


## 9. UI gotchas worth designing around

- **Name coupling.** Dashboard discovery reads `dashboard/manifest.json.name`; the API
  mount gate compares it to `plugins.enabled` / `plugins.disabled`
  (`_plugin_api_mount_skip_reason`). Keep it identical to the native plugin's registry key
  (the `plugin.yaml` `name`, or the directory name). A mismatch yields a visible tab whose
  backend 404s.
- **Catalog `validate` does not count `dashboard/manifest.json` as a loadable entry.**
  `plugin_validate._LOADABLE_ENTRYPOINTS` is `("__init__.py", "desktop/plugin.js",
  "plugin.json")` and `_check_loadable` reports "nothing to load" otherwise. The install
  path is more lenient (`plugins_cmd_inspect._AGENT_PAYLOAD_FILES` includes
  `dashboard/manifest.json`). A published calendar plugin should ship a real `__init__.py`
  anyway (it needs tools), which satisfies both.
- **Assets are unauthenticated** (enabled-gated, allowlisted). Never put secrets in the
  JS bundle; fetch them from `plugin_api.py`.
- **The `api` file is imported as Python** by design. Keep it inside `dashboard/`; the
  server refuses absolute/escaping paths.
- **`hidden_plugins`** in `config.yaml` (`dashboard.hidden_plugins`) hides a tab without
  disabling the plugin; `POST /api/dashboard/plugins/{name}/visibility` toggles it.

## 10. Desktop app is a different SDK (and the wrong one here)

`~/.hermes/desktop-plugins/` exists and is empty. The Desktop SDK docs say a desktop
plugin is `$HERMES_HOME/desktop-plugins/<id>/plugin.js` — plain ESM, watched and loaded by
**the Desktop app process** on the machine running the app. Even with a remote backend,
the app loads the desktop half from its own `desktop-plugins/` (the remote box's
`~/.hermes/plugins` is not a reachable filesystem), which is client-side UI. That
contradicts "the UI runs on the Hermes host", so the calendar plugin's UI should be the
web dashboard. A thin desktop half can be added later as a nicety, not as the primary UI.

---

# Part 2 — Native code (Rust)

## 11. What the plugin system will and will not do

Guaranteed by the installer:

- Clone a repo (or catalog pin) into `~/.hermes/plugins/<name>/`.
- Read `pyproject.toml` (preferred) or `python_dependencies` / `pip_dependencies`.
- Ask for dependency consent, then run PM (`uv`) admission at enable time; on failure the
  previous environment and plugin selection are preserved.
- Show `after-install.md` if present (`_display_after_install`).
- Record provenance in `.install-metadata.json`.

Not available:

- No arbitrary install/post-install script. Grepping `plugins_cmd_install.py` for
  `setup.sh`/`post_install`/`chmod` finds only `after-install.md`, a read-only chmod on the
  install tree, and no execution hook.
- No automatic download of GitHub release assets.
- No Rust toolchain provisioning on Linux/macOS. `pm/build_compilers.py` only bridges a
  missing `clang`/`clang++` to `gcc`/`g++` and `AR` for **C/C++ sdist builds**;
  `pm/native_build.py` provisions MSVC/Clang/Rust **only for Windows ARM64** from
  `scripts/build/windows-deps.ps1`.

So a Rust component has to arrive as (a) a Python wheel from an index, (b) a source build
that uv performs, (c) a binary the plugin fetches/builds itself, or (d) a separate MCP
server.

## 12. Option A — PyO3 extension as a published wheel

This is the cleanest fit for "publish for others to install".

`plugin.yaml`:

```yaml
name: hermes-calendar
version: 0.1.0
manifest_version: 2
python_dependencies:
  - "hermes-calendar-native>=0.1.0,<0.2"
```

or a sibling `pyproject.toml` (which wins over the manifest):

```toml
[project]
name = "hermes-calendar"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["hermes-calendar-native>=0.1.0,<0.2"]
```

How it is handled (`pm/plugin_declarations.py`, `pm/publication.py`):

- `applicable_requirements` keeps **index requirements only** — it drops requirements with
  a URL and anything named `hermes-agent`.
- `read_python_declaration` returns the effective surface for consent, admission, builds
  and currency; a `[project]` table owns packaging.
- `installs_packages` is true when there are requirements, so the plugin becomes a **uv
  workspace member** and its deps join the shared environment generation.
- Publication is transactional: uv builds a fresh generation, then env + config swap
  together; a failure keeps the old selection.
- Markers survive to uv (`applicable_requirements` keeps them for the target interpreter).

What the user needs on the host:

- A wheel matching `cp3XX` / OS / arch. Python here is **3.14.9** (the pinned
  `~/.hermes/tools/python-3.14.9+...`), so wheels must exist for cp314.
- Nothing else. No Rust.
- If no wheel exists for the target, uv falls back to the sdist and the build needs
  `cargo`/`rustc`, a C linker (`clang`/`gcc`), and time. This host has `gcc`, `cc`,
  `clang` — but **no `cargo`, `rustc`, or `maturin`**.

Catalog treatment: the plugin tree contains only source and a version range, which rule 17
allows. Reviewers see the dependency at the pin. The trust question moves to PyPI: the docs
are explicit that *"The plugin's author, not Hermes, is responsible for what that pulls
in"* and that Hermes's 3-day release-age quarantine applies to **Hermes's own**
dependencies, not to a plugin's. Pin upper bounds and publish the wheel from CI.

## 13. Option B — maturin build from the plugin repo itself

Mechanically this works because `read_python_declaration.installs_packages` counts a
`pyproject.toml` with a `[build-system]` table (or `[tool.uv] package = true`, optional
dependency groups, …). The plugin directory then becomes a workspace member, and uv builds
it from source during `pm.sync_venv(...)` — which is exactly what
`hermes plugins validate --install-deps` calls:

```python
# hermes_cli/plugins_cmd_catalog.py::cmd_validate
pm.sync_venv(plugins=Candidates([Path(path)]))
# then probe with that environment's interpreter
```

Shape constraints:

- The plugin still needs a root `__init__.py` with `register(ctx)`. A pure maturin layout
  (`python/` source root, module built by maturin) does not provide that by itself; ship a
  thin wrapper `__init__.py` that imports the compiled module and registers tools.
- `requires-python` must admit 3.14 (`PythonDeclaration.python_error` refuses a member whose
  `requires-python` excludes the interpreter, and uv intersects every member's specifier).
- Every enable/update rebuilds the member; `uv.lock`/environment stamps are per generation.

Host requirements: `cargo` + `rustc` (absent now), a linker, and uv fetching `maturin` as a
build requirement. On a pip-installed user machine this is a first-run failure with a
compiler error, not a friendly install message.

Catalog: legal (source only), but a poor fit for "others install it" because most users
have no Rust toolchain. Prefer A.

## 14. Option C — sidecar binary run as a subprocess

The documented escape hatch is `python_runtime: external`:

> `external` — the plugin manages its own interpreter/venv (sidecar pattern); Hermes
> installs nothing and leaves any `pyproject.toml` alone.

(`website/docs/developer-guide/plugins/index.md`, manifest v2 reference.)

With it, `PythonDeclaration.external` is true → `install_requirements` is empty,
`installs_packages` is false, no dependency consent, no workspace membership, and
`plugin_validate` reports `python dependencies: external runtime (plugin manages its own)`.
The plugin's Python then launches the binary with `subprocess` (the scanner rates
`subprocess.*` as **medium**, which alone is a `safe` verdict).

Where the binary lives: **not** in the plugin tree (that is rule 17). The sanctioned home
is `PLUGIN_DATA` (`plugin_data_dir(name)` → `<HERMES_HOME>/plugin-data/<name>/`), which
survives `hermes plugins update` / `remove` and follows the active profile. Acquire it at
first use or from a plugin CLI command (`ctx.register_cli_command`, e.g.
`hermes calendar setup`), never at import/`register()` time: that code runs inside the
validation probe (catalog CI), and `hermes plugins doctor` blocks sockets during it.

Catalog rule 17 (`website/docs/developer-guide/plugins/catalog-submission.md`, mirrored in
`plugin-catalog/README.md`):

> A plugin must not ship a compiled binary (a native executable or library of any OS —
> detected by file header, not extension: ELF, Mach-O, PE/`MZ`, WebAssembly), committed
> Python bytecode … If a plugin genuinely needs a native component, the binary must be
> built by the plugin repository's own public GitHub Actions from source in that repository
> and carry a GitHub build attestation (`actions/attest-build-provenance`) tying it to that
> workflow and commit; the gate verifies the attestation against the pinned commit
> (`gh attestation verify --repo <owner/repo> --source-digest <pinned sha>`). A binary a
> user's own machine fetches at setup time is allowed only from the upstream tool's
> official source, pinned by checksum, and disclosed (rule 13) — never bundled and never
> from an unofficial mirror.

So: shipping `bin/calendar-x86_64` in the repo is out. Shipping a `setup` that downloads
your own GitHub release asset, built and attested by your repo's CI, matches the rule's
intent (and the release asset is not "bundled"); a third-party binary must come from that
tool's official source with a pinned checksum and a disclosure line.

Host requirements: correct OS/arch/libc (linux-x64 glibc here), execute permission on the
downloaded file, and whatever dynamic libraries the binary links (rustls-static builds avoid
OpenSSL). `PLUGIN_DATA` is writable; the install tree is not guaranteed to be.

## 15. Option D — MCP server

Two distinct registration paths:

**Portable Agent Plugins v1 package** — root `mcp.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "calendar": {
      "type": "stdio",
      "command": "./bin/calendar-mcp",
      "args": ["--data", "${PLUGIN_DATA}"],
      "env": { "CALENDAR_TOKEN": "${CALENDAR_TOKEN}" }
    }
  }
}
```

The top-level object must be **exactly** `{"$schema", "mcpServers"}` and `$schema` must
equal that URL (`_discover_mcp`); anything else is a load diagnostic and the package's MCP
servers are skipped.

Parsing rules (`hermes_cli/agent_plugins.py::_translate_stdio`):

- Allowed fields: `type`, `command`, `args`, `env`, `cwd`.
- `command` must be **one executable token**: a bare name on `PATH` or a `./...` path
  resolved inside `PLUGIN_ROOT`. A relative path cannot escape the package.
- `args`, `env` values, and `cwd` expand `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` through
  `_resolve_scoped_path`, which refuses paths escaping their root. `PLUGIN_ROOT` and
  `PLUGIN_DATA` are reserved env keys and are injected.
- `${PLUGIN_DATA}/...` is **not accepted as `command`** — only `./` or a bare name. A
  downloaded binary therefore needs either a bare name on `PATH` or a small committed
  launcher script (`./bin/run.sh`, a text file) that execs the real binary.
- Env pass-through: write `"NAME": "${NAME}"` to forward a user variable; unset/empty
  means the entry is ignored. Package `env` values are visible data, not a secret store.
- `streamable-http` entries work through Hermes's remote MCP client: absolute http(s) URL,
  no user info or fragment, plain HTTP only for loopback.
- Legacy `sse` entries are reported and skipped.
- Portable packages are **disabled after install** until enabled; MCP servers arrive after
  `mcp.reload` for a mid-run install (`deferred.mcp_servers`).

**Native plugin** — `plugin.yaml` has no `mcp_servers` field (`_KNOWN_MANIFEST_FIELDS` in
`plugins_manifest.py`). The only MCP surface for a native plugin is the user's own
`mcp_servers` block in `config.yaml`, or the plugin running the server itself via
`subprocess`/`ctx.call_mcp` for the client side. A plugin can also declare a portable
package alongside, but that is a separate install unit.

Catalog: same rule 17. A Rust MCP server binary is a "native component" and must be
built/attested or fetched from its official source. A **remote** streamable-http MCP
server is the exception with no local binary at all — but that puts the calendar data
off-host, which conflicts with the owner decision.

## 16. Dependency handling, precisely

| Behavior | Detail | Source |
|---|---|---|
| Declaration precedence | `pyproject.toml` `[project].dependencies` wins over manifest `python_dependencies` / `pip_dependencies` | `pm/plugin_declarations.py` |
| pyproject with no installs | `dependencies = []`, no `[build-system]`, no extras/groups → "no declaration": no consent, no member | `plugins/index.md`; `installs_packages` |
| Direct URLs | Dropped from install (`applicable_requirements`) and warned about by `validate` ("not managed by PM; use a plugin-owned external runtime") | `plugin_validate._check_python_dependencies` |
| `hermes-agent` requirement | Dropped — the checkout supplies Hermes | `applicable_requirements` |
| Malformed PEP 508 | Refused at declaration read; `validate` fails the check | `plugin_declarations.py` |
| `requires-python` | Intersected across members; one excluding member fails the whole lock | `PythonDeclaration.python_error` |
| Resolution | uv, one shared environment generation for core + extras + enabled plugins | `pm/AGENTS.md`, `pm/install.py` |
| Atomicity | New generation prepared, then env + config published together; failure preserves the previous selection | `plugins/index.md`; `pm/publication.py` |
| Wording of consent | `--yes-deps` answers it; `--no-deps` installs disabled and cannot replace an active plugin | `plugins install --help` |
| `--install-deps` | Runs a real `pm.sync_venv` with the plugin as a candidate member, then probes `register()` from that environment | `plugins_cmd_catalog.py::cmd_validate` |
| Release-age quarantine | Applies to Hermes's own deps (`[tool.uv] exclude-newer = "3 days"`), **not** the plugin's | `plugins/index.md` §Dependency security policy |
| No install scripts | Only `after-install.md` is displayed | `plugins_cmd_install.py` |

## 17. What the scanner and catalog actually catch

Install scanner (`tools/plugin_guard.py`, reusing `tools/skills_guard.py`):

- `SUSPICIOUS_BINARY_EXTENSIONS = {.exe, .dll, .so, .dylib, .bin, .dat, .com, .msi, .dmg,
  .app, .deb, .rpm}` → a `binary_file` finding, remapped to **high** for plugins.
  `_determine_verdict` maps critical→`dangerous`, high→`caution`. So a bundled `.so` yields
  **caution**: install prompts for confirmation, and `--force` overrides caution (never
  `dangerous`).
- Extension-based, not header-based. An extensionless ELF would slip past the automated
  scanner; catalog rule 17 says reviewers detect by header. Do not rely on the scanner as
  the only control.
- Committed `.pyc`/`.pyo` → `bytecode_file`, also remapped to high (caution).
- `subprocess.*` → medium; unpinned `pip install` → medium. Medium alone is "safe".
- Structural limits: file count and total size produce medium findings.

Catalog admission is `hermes plugins validate <path> --install-deps` in CI plus human
review of the pinned 40-hex commit. There is **no automated attestation verification in
`hermes plugins install`** — grepping `hermes_cli/` for `attestation` finds nothing in the
install/catalog path. Rule 17 is enforced by reviewers and by the catalog CI gate, not at
install time on a user's machine. That is another reason to prefer Option A: a PyPI wheel
dependency is inspectable (name + version) at the pin and needs no binary in the tree.

## 18. Recommendation for the calendar plugin

Given "published" + "UI on the host" + "part in Rust":

1. **UI:** web-dashboard tab in the same plugin folder (`dashboard/manifest.json`,
   `dist/index.js`, `dashboard/plugin_api.py`), with a `cron:top` or `pre-main` summary
   card. No separate frontend deploy, no Desktop SDK.
2. **Rust:** only for the piece that truly needs it (e.g. RFC 5545 recurrence expansion,
   timezone math, an iCal parser). Publish it as a **PyO3 wheel on PyPI** for cp311-cp314
   on linux/macos/windows, and declare a bounded range. Keep a pure-Python fallback behind
   `try: import hermes_calendar_native except ImportError` so installs on an exotic
   platform still work — that also keeps `register()` importable during the validation
   probe.
3. **Do not** ship a binary in the repo; **do not** make the plugin a maturin source build
   (most users lack Rust); **do not** download a binary at import time.
4. If a sidecar is unavoidable later, use `python_runtime: external` + a `hermes calendar
   setup` CLI command that fetches the attested release asset into `plugin_data_dir()`,
   verifies a pinned SHA-256, and records the version.
5. **Add a `__init__.py`** even if the UI is the main surface: catalog `validate`'s loadable
   check does not count `dashboard/manifest.json`.
6. Keep the dashboard `name`, `plugin.yaml` `name`, and directory name identical.

## 19. Open questions

- **Dashboard credential.** Confirm where the dashboard login credential lives on the target
  deployment before depending on remote access; a restart can re-evaluate it and fail closed.

- **Publishing tier.** Catalog admission requires a public repo, a 40-hex pin and reviewer
  sign-off per update (`catalog-submission.md`). Decide whether the calendar plugin targets
  the catalog or "install from Git" only; the latter skips review but users get a
  caution-prompt install if anything is flagged.
- **Wheel platform matrix.** cp314 wheels are the constraint here; confirm which platforms
  the owner wants to support before choosing PyO3 (manylinux + macOS universal2 + Windows
  is three CI jobs, and abi3 can cut it to one wheel per OS).
- **Data location.** `plugin_data_dir()` is profile-scoped; if events must be shared across
  profiles, that is a design decision the plugin API does not solve for you.
- **Calendar backend.** Whether the Rust part talks to CalDAV/Google/ICS also decides
  whether `requires_env`/`oauth:` (native manifest OAuth, documented in the first report)
  or a local store is the right credential path.
