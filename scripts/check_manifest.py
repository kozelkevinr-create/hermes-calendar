#!/usr/bin/env python3
"""Structural checks for the Hermes plugin contract.

This is the offline subset of ``hermes plugins validate``: it verifies the
manifest, the entry point beside it, and version consistency. CI runs it on
every PR. When Hermes is installed, ``make plugin-validate`` also runs the real
validator (manifest drift, capability probe, hook/tool declarations).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = ("name", "version", "description")
ENTRY_POINT = "from .hermes_calendar.plugin import register"


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)


def check_manifest() -> bool:
    manifest_path = REPO_ROOT / "plugin.yaml"
    if not manifest_path.is_file():
        fail("plugin.yaml is missing")
        return False

    data: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("plugin.yaml does not contain a mapping")
        return False

    ok = True
    for field in REQUIRED_FIELDS:
        if not data.get(field):
            fail(f"plugin.yaml is missing required field: {field}")
            ok = False

    entry = REPO_ROOT / "__init__.py"
    if not entry.is_file():
        fail("__init__.py is not beside plugin.yaml; Hermes will not load the plugin")
        ok = False
    elif ENTRY_POINT not in entry.read_text(encoding="utf-8"):
        fail("__init__.py does not import the plugin register() entry point")
        ok = False

    version = data.get("version")
    if isinstance(version, str):
        package_init = (REPO_ROOT / "hermes_calendar" / "__init__.py").read_text(encoding="utf-8")
        if f'__version__ = "{version}"' not in package_init:
            fail(f"plugin.yaml version {version} does not match hermes_calendar.__version__")
            ok = False
    return ok


def main() -> int:
    if check_manifest():
        print("plugin manifest: OK")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
