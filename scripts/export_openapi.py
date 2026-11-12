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

from fastapi import FastAPI

from hermes_calendar.ui.dashboard_api import API_VERSION, router

REPO_ROOT = Path(__file__).resolve().parents[1]
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
            existing = destination.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(
                f"stale: {destination} is missing; run scripts/export_openapi.py", file=sys.stderr
            )
            return 1
        if existing != document:
            print(
                f"stale: {destination} differs; run scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"up to date: {destination}")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
