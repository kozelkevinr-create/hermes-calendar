"""Manifest and entry-point contract tests.

These mirror the checks ``hermes plugins validate`` performs, without requiring
a Hermes install, so CI catches a broken manifest on every PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hermes_calendar import __version__
from hermes_calendar.plugin import register

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_manifest() -> dict[str, Any]:
    data = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_manifest_has_required_fields() -> None:
    manifest = load_manifest()
    for field in ("name", "version", "description"):
        assert manifest.get(field), f"plugin.yaml is missing {field}"


def test_manifest_version_matches_package() -> None:
    assert load_manifest()["version"] == __version__


def test_entry_point_sits_beside_manifest() -> None:
    entry = REPO_ROOT / "__init__.py"
    assert entry.is_file()
    assert "from .hermes_calendar.plugin import register" in entry.read_text(encoding="utf-8")


def test_register_is_callable() -> None:
    assert callable(register)


def test_declared_surfaces_are_lists() -> None:
    manifest = load_manifest()
    assert isinstance(manifest.get("provides_tools"), list)
    assert isinstance(manifest.get("provides_hooks"), list)


def test_calendar_status_is_declared() -> None:
    assert "calendar_status" in load_manifest()["provides_tools"]
