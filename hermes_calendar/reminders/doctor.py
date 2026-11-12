"""Headless diagnostics for the reminder delivery path.

MVP acceptance check 11 says ``doctor`` exits non-zero when the default external
channel is missing or broken. The checks stay deliberately read-only: they do not
send a test message and never print a target or calendar content.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..db import coordination
from ..db.reminders import ReminderStore


@dataclass(frozen=True)
class DoctorCheck:
    """One named check with a non-secret explanation."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    """The aggregate doctor result; ``ok`` is the process exit contract."""

    ok: bool
    checks: tuple[DoctorCheck, ...]


def run_doctor(
    connection: sqlite3.Connection,
    *,
    global_connection: sqlite3.Connection | None = None,
    binary: str = "hermes",
    which: Callable[[str], str | None] | None = None,
) -> DoctorReport:
    """Run the headless checks against an opened profile (and global) store."""
    checks: list[DoctorCheck] = []
    checks.append(DoctorCheck("profile_store", True, "profile store opened"))

    store = ReminderStore(connection)
    chat_channels = [
        channel for channel in store.channels() if channel.kind == "hermes_send" and channel.enabled
    ]
    configured = [
        channel
        for channel in chat_channels
        if isinstance(channel.config.get("target"), str) and str(channel.config["target"]).strip()
    ]
    if not chat_channels:
        checks.append(
            DoctorCheck(
                "external_channel",
                False,
                "no enabled hermes_send channel; reminders are in-app only",
            )
        )
    elif not configured:
        checks.append(
            DoctorCheck(
                "external_channel",
                False,
                "the enabled hermes_send channel has no explicit target",
            )
        )
    else:
        checks.append(DoctorCheck("external_channel", True, "explicit target configured"))

    find = shutil.which if which is None else which
    present = bool(find(binary)) or Path(binary).is_file()
    checks.append(
        DoctorCheck(
            "hermes_binary",
            present,
            f"{binary!r} is available" if present else f"{binary!r} is not on PATH",
        )
    )

    if global_connection is None:
        checks.append(
            DoctorCheck(
                "global_lease",
                True,
                "global store unavailable; profile reminders still work",
            )
        )
    else:
        try:
            lease = coordination.read_lease(global_connection)
        except sqlite3.Error as exc:
            checks.append(
                DoctorCheck(
                    "global_lease",
                    False,
                    f"global coordination unavailable: {type(exc).__name__}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "global_lease",
                    True,
                    "unheld" if lease is None else f"held by profile {lease.owner_profile_id}",
                )
            )

    essential = {"profile_store", "external_channel", "hermes_binary"}
    return DoctorReport(
        ok=all(check.ok for check in checks if check.name in essential),
        checks=tuple(checks),
    )
