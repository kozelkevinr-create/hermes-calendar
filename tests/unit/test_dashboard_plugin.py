"""Static and load-time checks for the hand-written dashboard bundle.

There is no Node test runner in this project, so the bundle is treated as data:
the manifest must point at real files, the JavaScript must register the plugin
under the same name as the agent manifest, and the source must not contain the
DOM/JavaScript escape hatches that would make event text executable. The
backend bridge is also loaded both in-process and in an isolated interpreter,
because Hermes imports it by path rather than as part of the package.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "dashboard"
MANIFEST = DASHBOARD / "manifest.json"
BUNDLE = DASHBOARD / "dist" / "index.js"
STYLESHEET = DASHBOARD / "dist" / "style.css"
API_FILE = DASHBOARD / "plugin_api.py"

FORBIDDEN_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "dangerouslySetInnerHTML",
    "document.write",
    "eval(",
    "new Function(",
)


def _manifest() -> dict[str, Any]:
    decoded: Any = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def test_manifest_matches_the_agent_plugin_and_points_at_real_files() -> None:
    manifest = _manifest()
    agent: Any = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(agent, dict)

    assert manifest["name"] == agent["name"] == "hermes-calendar"
    assert manifest["label"]
    assert manifest["tab"]["path"] == "/calendar"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["css"] == "dist/style.css"
    assert manifest["api"] == "plugin_api.py"

    for relative in (manifest["entry"], manifest["css"], manifest["api"]):
        candidate = (DASHBOARD / relative).resolve()
        assert candidate.is_file()
        assert candidate.is_relative_to(DASHBOARD.resolve())


def test_bundle_uses_the_plugin_sdk_and_never_builds_dom_from_event_text() -> None:
    source = BUNDLE.read_text(encoding="utf-8")

    assert "window.__HERMES_PLUGIN_SDK__" in source
    assert "window.__HERMES_PLUGINS__.register(PLUGIN_NAME, CalendarPage)" in source
    assert "SDK.fetchJSON" in source
    assert "/api/plugins/" in source

    present = [token for token in FORBIDDEN_JS if token in source]
    assert not present, f"bundle uses forbidden DOM/JS escape hatch(es): {present}"

    # The notification panel belongs to the reminders task; this bundle must
    # leave a visibly labelled empty slot rather than fetch reminder data.
    assert "Notification panel placeholder" in source
    assert "/notifications" not in source

    # Required controls and labels, checked as data because there is no JS test
    # runner in this project.
    for identifier in (
        "hc-summary",
        "hc-start-date",
        "hc-start-time",
        "hc-end-date",
        "hc-end-time",
        "hc-timezone",
        "hc-allday",
        "hc-recurrence",
        "hc-count",
        "hc-until",
        "hc-calendar",
        "hc-scope",
    ):
        assert identifier in source
    for preset in ('"daily"', '"weekly"', '"monthly"', '"yearly"'):
        assert preset in source
    assert "Profile (private)" in source
    assert "Shared (global)" in source
    assert "aria-live" in source
    assert "textContent" in source


def test_stylesheet_is_hand_written_and_exposes_focus_styles() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")

    assert source.strip()
    assert ":focus-visible" in source
    assert "--color-border" in source


def test_dashboard_api_bridge_exposes_the_router() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_calendar_test", API_FILE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    routes = {route.path for route in module.router.routes}
    assert "/calendars" in routes
    assert "/events" in routes
    assert "/events/{event_id}" in routes
    assert "/status" in routes


def test_bridge_loads_without_the_repository_on_sys_path(tmp_path: Path) -> None:
    driver = """
import importlib.util
import pathlib
import sys

api = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_isolated", api)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(sorted({route.path for route in module.router.routes}))
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", driver, str(API_FILE)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "/calendars" in result.stdout
    assert "/status" in result.stdout


def test_register_and_validation_work_without_fastapi(tmp_path: Path) -> None:
    driver = """
import builtins
import sys

real_import = builtins.__import__

def blocked(name, *args, **kwargs):
    if name == "fastapi" or name.startswith("fastapi."):
        raise ImportError("fastapi is intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked
sys.path.insert(0, sys.argv[1])

import hermes_calendar.plugin as plugin

class Context:
    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs["name"])

context = Context()
plugin.register(context)
assert "calendar_status" in context.tools

try:
    import hermes_calendar.ui.dashboard_api  # noqa: F401
except ImportError:
    print("dashboard api is guarded")
else:
    raise SystemExit("dashboard api imported without fastapi")

print("register ok")
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", driver, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "dashboard api is guarded" in result.stdout
    assert "register ok" in result.stdout
