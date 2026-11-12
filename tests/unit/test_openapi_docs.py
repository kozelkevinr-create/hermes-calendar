"""The checked-in OpenAPI document must match the router it documents.

The exporter is run as a subprocess with the checkout on PYTHONPATH, exactly as
CI runs from a clean checkout with no editable install. The stale check compares
the normalized contract (paths, methods, parameters, required flags, response
status codes), not raw bytes, because FastAPI/Pydantic releases add schema
details without changing this plugin's routes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "export_openapi.py"
DOCUMENT = REPO_ROOT / "docs" / "api" / "openapi.json"
PREFIX = "/api/plugins/hermes-calendar"


def _environment() -> dict[str, str]:
    """Return an environment that makes the checkout importable without installing it."""
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(REPO_ROOT) if not existing else f"{REPO_ROOT}{os.pathsep}{existing}"
    )
    return environment


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the exporter as a subprocess from the repository root."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=str(REPO_ROOT),
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


def _export() -> subprocess.CompletedProcess[str]:
    """Render the current OpenAPI document to stdout."""
    return _run("--output", "-")


def test_checked_in_openapi_is_not_stale() -> None:
    result = _run("--check")

    assert result.returncode == 0, result.stderr or result.stdout
    assert "structural form" in result.stdout
    # The full document stays checked in and must remain valid JSON.
    decoded: Any = json.loads(DOCUMENT.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)


def test_openapi_is_deterministic_and_lists_every_route() -> None:
    first = _export()
    second = _export()

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
