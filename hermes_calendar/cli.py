"""Command-line interface for ``hermes-calendar``.

The CLI is script-friendly: diagnostics go to stderr, machine-readable output
is opt-in via ``--json``, and the exit code is the API. Subcommands
(``reminders tick``, ``reminders doctor``, ``db migrate``) are added by their
subsystems; the scaffold only pins the entry point and ``--version``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from hermes_calendar import __version__

PROGRAM = "hermes-calendar"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Local-first calendar for Hermes Agent.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the plugin version and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version:
        print(f"{PROGRAM} {__version__}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through __main__.py
    raise SystemExit(main())
