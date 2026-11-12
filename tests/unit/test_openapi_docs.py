"""The checked-in OpenAPI document must match the router it documents."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "export_openapi.py"
DOCUMENT = REPO_ROOT / "docs" / "api" / "openapi.json"
PREFIX = "/api/plugins/hermes-calendar"


def test_checked_in_openapi_is_not_stale() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", "-"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert DOCUMENT.read_text(encoding="utf-8") == result.stdout


def test_openapi_is_deterministic_and_lists_every_route() -> None:
    first = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", "-"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", "-"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    decoded: Any = json.loads(first.stdout)
    assert isinstance(decoded, dict)
    schema = cast(dict[str, Any], decoded)
    paths = cast(dict[str, Any], schema["paths"])
    assert set(paths) == {
        PREFIX + "/calendars",
        PREFIX + "/calendar",
        PREFIX + "/events",
        PREFIX + "/events/{event_id}",
        PREFIX + "/import",
        PREFIX + "/export",
        PREFIX + "/status",
        PREFIX + "/version",
        PREFIX + "/notifications",
        PREFIX + "/notifications/{notification_id}/dismiss",
        PREFIX + "/notifications/{notification_id}/snooze",
        PREFIX + "/reminders/status",
    }
    assert schema["info"]["version"] == "1"
