"""Pin the shipped plugin identity.

Copying a repository can leave another plugin's name in ``plugin.yaml``, and
``hermes plugins validate`` does not notice. A real install would then register
under the wrong name, so the name is asserted explicitly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_NAME = "hermes-calendar"


def test_plugin_yaml_name_is_the_shipped_name() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == EXPECTED_NAME


def test_dashboard_manifest_name_matches_plugin_yaml() -> None:
    import json

    dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dashboard["name"] == EXPECTED_NAME


def test_pyproject_name_and_version_match_plugin_yaml() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == EXPECTED_NAME
    assert project["version"] == str(manifest["version"])
