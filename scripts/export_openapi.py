#!/usr/bin/env python3
"""Export the dashboard plugin's OpenAPI document deterministically.

The checked-in ``docs/api/openapi.json`` is the machine-readable companion to
``docs/api/dashboard-api.md``. It is built from the same FastAPI router Hermes
mounts at ``/api/plugins/hermes-calendar/`` and serialized with sorted keys and
a trailing newline, so a regeneration only changes bytes when the routes do.

Usage:
    python scripts/export_openapi.py             # write docs/api/openapi.json
    python scripts/export_openapi.py --check     # fail if the file is stale
    python scripts/export_openapi.py --output -  # write to stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Running a script puts ``scripts/`` on sys.path, not the repository root. Hermes
# imports the plugin by path and the tests may run from a checkout without an
# editable install, so add the repository root before importing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402 - path bootstrap above

from hermes_calendar.ui.dashboard_api import API_VERSION, router  # noqa: E402 - path bootstrap above

DEFAULT_OUTPUT = REPO_ROOT / "docs" / "api" / "openapi.json"
PREFIX = "/api/plugins/hermes-calendar"


def build_app() -> FastAPI:
    """Build the app Hermes exposes, with the real mount prefix."""
    app = FastAPI(
        title="Hermes Calendar dashboard API",
        version=API_VERSION,
        description=(
            "Stable contract for the Hermes Calendar dashboard plugin routes. "
            "All /api/* routes use the Hermes dashboard authentication "
            "(X-Hermes-Session-Token or Bearer in token mode, session cookie in "
            "password-protected mode); the plugin never sees credentials. "
            "Within api_version 1 changes are additive only."
        ),
    )
    app.include_router(router, prefix=PREFIX)
    return app


def render_openapi() -> str:
    """Return the canonical JSON text for the current router."""
    schema: Any = build_app().openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def structural_form(schema: dict[str, Any]) -> dict[str, Any]:
    """Reduce an OpenAPI document to the contract facts CI must guard.

    FastAPI and Pydantic add or move schema details between releases (for
    example the ``ValidationError.ctx``/``input`` properties), so byte equality
    would fail without a route change. Paths, methods, parameter names and
    locations, their required flags, and response status codes are the facts
    this repository actually owns.
    """
    raw_paths = schema.get("paths")
    paths: dict[str, Any] = raw_paths if isinstance(raw_paths, dict) else {}
    normalized: dict[str, Any] = {}
    for path, raw_methods in paths.items():
        if not isinstance(raw_methods, dict):
            continue
        methods: dict[str, Any] = raw_methods
        for method, operation in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = [
                {
                    "name": parameter.get("name"),
                    "in": parameter.get("in"),
                    "required": bool(parameter.get("required", False)),
                }
                for parameter in operation.get("parameters", [])
                if isinstance(parameter, dict)
            ]
            entry: dict[str, Any] = {
                "parameters": sorted(
                    parameters,
                    key=lambda parameter: (
                        str(parameter["in"]),
                        str(parameter["name"]),
                    ),
                ),
                "responses": sorted(str(status) for status in operation.get("responses", {})),
            }
            body = operation.get("requestBody")
            if isinstance(body, dict):
                entry["request_body_required"] = bool(body.get("required", False))
            normalized[f"{method.upper()} {path}"] = entry
    info = schema.get("info")
    api_version = info.get("version") if isinstance(info, dict) else None
    return {"api_version": api_version, "routes": normalized}


def render_structural(schema: dict[str, Any]) -> str:
    """Serialize :func:`structural_form` canonically for comparison."""
    return json.dumps(structural_form(schema), indent=2, sort_keys=True) + "\n"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="destination path, or '-' for stdout (default: docs/api/openapi.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 when the destination differs from a fresh export",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Write, print, or verify the generated OpenAPI document."""
    args = _parse_args(argv)
    document = render_openapi()
    if args.output == "-":
        sys.stdout.write(document)
        return 0
    destination = Path(str(args.output))
    if args.check:
        try:
            existing_text = destination.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(
                f"stale: {destination} is missing; run scripts/export_openapi.py", file=sys.stderr
            )
            return 1
        try:
            existing_schema: Any = json.loads(existing_text)
            fresh_schema: Any = json.loads(document)
        except json.JSONDecodeError as exc:
            print(f"stale: {destination} is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(existing_schema, dict) or not isinstance(fresh_schema, dict):
            print(f"stale: {destination} is not an OpenAPI object", file=sys.stderr)
            return 1
        if render_structural(existing_schema) != render_structural(fresh_schema):
            print(
                f"stale: {destination} routes/parameters differ; run scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"up to date: {destination} (structural form)")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
