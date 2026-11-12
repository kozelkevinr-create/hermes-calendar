"""Command-line interface for ``hermes-calendar``.

The CLI is script-friendly: diagnostics go to stderr, machine-readable output
is opt-in via ``--json``, and the exit code is the API. ``reminders tick`` is the
entry point the per-profile ``hermes cron`` job calls; ``reminders cron-command``
prints (but never installs) the exact job command.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from collections.abc import Sequence

from . import __version__
from .db.store import open_global_store, open_profile_store
from .reminders.cron import cron_command, tick_script
from .reminders.doctor import DoctorReport, run_doctor
from .reminders.engine import ProfileMismatchError
from .reminders.runtime import run_tick

PROGRAM = "hermes-calendar"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Local-first calendar for Hermes Agent.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the plugin version and exit",
    )
    commands = parser.add_subparsers(dest="command")

    reminders = commands.add_parser(
        "reminders",
        help="run or inspect the reminder scheduler",
    )
    reminder_commands = reminders.add_subparsers(dest="reminders_command")

    tick = reminder_commands.add_parser(
        "tick",
        help="claim and deliver every due reminder for the active profile",
    )
    tick.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing on success (use this from cron)",
    )
    tick.add_argument(
        "--json",
        action="store_true",
        help="print the tick counters as JSON",
    )

    reminder_commands.add_parser(
        "cron-command",
        help="print the exact hermes cron command to run manually",
    )
    doctor = reminder_commands.add_parser(
        "doctor",
        help="check the profile store and the external delivery channel",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="print the doctor report as JSON",
    )
    return parser


def _tick(*, quiet: bool, as_json: bool) -> int:
    try:
        result = run_tick()
    except ProfileMismatchError as exc:
        print(f"reminders tick: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(
            f"reminders tick: could not run: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if quiet:
        return 0
    if as_json:
        print(json.dumps(dataclasses.asdict(result), sort_keys=True))
        return 0
    print(
        "reminders: "
        f"claimed={result.claimed} delivered={result.delivered} "
        f"retrying={result.retrying} suppressed={result.suppressed} "
        f"cancelled={result.cancelled} global_lease={result.global_lease_holder}"
    )
    if result.global_error:
        print(f"reminders: {result.global_error}", file=sys.stderr)
    return 0


def _doctor(*, as_json: bool) -> int:
    profile_connection = open_profile_store()
    global_connection: sqlite3.Connection | None = None
    try:
        try:
            global_connection = open_global_store()
        except (sqlite3.Error, OSError):
            global_connection = None
        report: DoctorReport = run_doctor(profile_connection, global_connection=global_connection)
    finally:
        if global_connection is not None:
            global_connection.close()
        profile_connection.close()

    if as_json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "checks": [dataclasses.asdict(check) for check in report.checks],
                },
                sort_keys=True,
            )
        )
    else:
        for check in report.checks:
            status = "ok" if check.ok else "FAIL"
            print(f"{status}: {check.name}: {check.detail}")
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version:
        print(f"{PROGRAM} {__version__}")
        return 0

    if args.command == "reminders":
        if args.reminders_command == "tick":
            return _tick(quiet=bool(args.quiet), as_json=bool(args.json))
        if args.reminders_command == "doctor":
            try:
                return _doctor(as_json=bool(args.json))
            except (sqlite3.Error, OSError) as exc:
                print(
                    f"reminders doctor: could not run: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return 1
        if args.reminders_command == "cron-command":
            print(cron_command())
            print(
                "reminders: write this script to the path hermes cron expects, "
                "then run the command above:",
                file=sys.stderr,
            )
            print(tick_script(), file=sys.stderr, end="")
            return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through __main__.py
    raise SystemExit(main())
