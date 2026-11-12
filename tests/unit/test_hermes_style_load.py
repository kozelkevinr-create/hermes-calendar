"""Load the plugin exactly as Hermes does and compare registered tools.

Hermes imports a user plugin directory by path as a package
(``spec_from_file_location(..., submodule_search_locations=[dir])``) and never
adds that directory to ``sys.path``. This test reproduces that load in a child
interpreter started with ``-I`` (no cwd, no ``PYTHONPATH``, no user site) so an
absolute ``hermes_calendar`` import cannot pass by accident.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_MODULE = "hermes_plugins_calendar"

_DRIVER = """
import importlib.util
import json
import pathlib
import sys

plugin_dir = pathlib.Path(sys.argv[1]).resolve()
module_name = sys.argv[2]
expected = json.loads(sys.argv[3])
repo_root = pathlib.Path(sys.argv[4]).resolve()

problems = []


class RecordingContext:
    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        name = kwargs.get("name")
        schema = kwargs.get("schema") or {}
        if schema.get("name") != name:
            problems.append("schema name does not match tool name: %r" % (name,))
        if not callable(kwargs.get("handler")):
            problems.append("handler is not callable: %r" % (name,))
        self.tools.append(name)


spec = importlib.util.spec_from_file_location(
    module_name,
    plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
if spec is None or spec.loader is None:
    problems.append("could not build an import spec for the plugin directory")
    print(json.dumps({"problems": problems}))
    raise SystemExit(1)

module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)

ctx = RecordingContext()
module.register(ctx)

registered = sorted(ctx.tools)
if registered != sorted(expected):
    problems.append(
        "registered tools %r do not match plugin.yaml provides_tools %r"
        % (registered, sorted(expected))
    )

stray = sorted(name for name in sys.modules if name.split(".")[0] == "hermes_calendar")
if stray:
    problems.append("loaded a top-level hermes_calendar package: %r" % (stray,))

if module.__package__ != module_name:
    problems.append("plugin __package__ is %r, expected %r" % (module.__package__, module_name))

on_path = [entry for entry in sys.path if entry and pathlib.Path(entry).resolve() == repo_root]
if on_path:
    problems.append("repository root leaked onto sys.path: %r" % (on_path,))

print(
    json.dumps(
        {
            "tools": registered,
            "module": module.__name__,
            "package": module.__package__,
            "problems": problems,
        }
    )
)
raise SystemExit(1 if problems else 0)
"""


def _declared_tools() -> list[str]:
    manifest: Any = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    tools = manifest.get("provides_tools")
    assert isinstance(tools, list)
    return [str(tool) for tool in tools]


def _copy_plugin(destination: Path) -> Path:
    """Copy just the files Hermes installs for this plugin."""
    plugin_dir = destination / PLUGIN_MODULE
    plugin_dir.mkdir()
    shutil.copy2(REPO_ROOT / "plugin.yaml", plugin_dir / "plugin.yaml")
    shutil.copy2(REPO_ROOT / "__init__.py", plugin_dir / "__init__.py")
    shutil.copytree(
        REPO_ROOT / "hermes_calendar",
        plugin_dir / "hermes_calendar",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return plugin_dir


def test_plugin_imports_as_a_package_without_the_repo_on_sys_path(tmp_path: Path) -> None:
    plugin_dir = _copy_plugin(tmp_path)
    expected = _declared_tools()

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _DRIVER,
            str(plugin_dir),
            PLUGIN_MODULE,
            json.dumps(expected),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["problems"] == []
    assert payload["module"] == PLUGIN_MODULE
    assert payload["package"] == PLUGIN_MODULE
    assert payload["tools"] == sorted(expected)
    assert "calendar_status" in payload["tools"]
