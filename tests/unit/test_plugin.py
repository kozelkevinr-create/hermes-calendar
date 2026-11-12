"""Plugin registration and calendar_status tool tests.

Hermes is not installed in the test environment, so the context is a stand-in
that records registrations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_calendar import __version__
from hermes_calendar.hermes.tools import TOOL_SPECS
from hermes_calendar.plugin import calendar_status, register

EXPECTED_TOOLS = {"calendar_status"} | {spec[0] for spec in TOOL_SPECS}


class FakeContext:
    """Minimal stand-in for Hermes ``PluginContext``."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


def test_register_registers_calendar_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "work"))
    ctx = FakeContext()

    register(ctx)

    assert {tool["name"] for tool in ctx.tools} == EXPECTED_TOOLS
    status = next(tool for tool in ctx.tools if tool["name"] == "calendar_status")
    assert status["toolset"] == "calendar"
    assert status["schema"]["name"] == "calendar_status"
    assert callable(status["handler"])


def test_calendar_status_reports_paths_and_missing_databases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "work"
    monkeypatch.setenv("HERMES_HOME", str(home))

    payload: dict[str, Any] = json.loads(calendar_status({}))

    assert payload["plugin"] == "hermes-calendar"
    assert payload["version"] == __version__
    assert payload["profile_data_dir"] == str(home / "plugin-data" / "hermes-calendar")
    assert payload["global_data_dir"] == str(tmp_path / "root" / "plugin-data" / "hermes-calendar")
    assert payload["databases"]["profile"]["exists"] is False
    assert payload["databases"]["global"]["exists"] is False
    assert Path(payload["databases"]["profile"]["path"]).name == "calendar.db"
    assert Path(payload["databases"]["global"]["path"]).name == "calendar-global.db"


def test_calendar_status_reports_existing_databases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "work"))
    first = json.loads(calendar_status({}))
    Path(first["databases"]["profile"]["path"]).touch()
    Path(first["databases"]["global"]["path"]).touch()

    payload: dict[str, Any] = json.loads(calendar_status())

    assert payload["databases"]["profile"]["exists"] is True
    assert payload["databases"]["global"]["exists"] is True
