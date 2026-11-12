"""Headless diagnostics for the reminder delivery path.

MVP acceptance check 11 says ``doctor`` exits non-zero when the default external
channel is missing or broken. The checks stay deliberately read-only: they do not
send a test message and never print a target or calendar content.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..db import coordination
from ..db.reminders import ReminderStore
from .config import load_settings
from .cron import JOB_NAME, SCHEDULE, cron_command, tick_script

CRON_FRESH_SECONDS = 180
LAST_TICK_PREF = "last_tick_utc"


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


def _rfc3339(epoch: int) -> str:
    """Format an epoch instant as the plugin's RFC 3339 UTC shape."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reminder_status(
    connection: sqlite3.Connection,
    *,
    global_connection: sqlite3.Connection | None = None,
    now: int | None = None,
    binary: str = "hermes",
    which: Callable[[str], str | None] | None = None,
) -> dict[str, object]:
    """Return the dashboard's read-only reminder setup snapshot.

    The payload reuses :func:`run_doctor` for the channel/binary/lease checks
    and adds scheduler facts that live in the profile's preferences. It never
    reveals a delivery target: only whether one is configured. The cron fields
    are informational; this function never creates a job or sends a message.
    """
    timestamp = int(time.time()) if now is None else int(now)
    report = run_doctor(
        connection,
        global_connection=global_connection,
        binary=binary,
        which=which,
    )
    store = ReminderStore(connection)
    settings = load_settings(store)
    raw_last_tick = store.get_pref(LAST_TICK_PREF)
    last_tick = (
        raw_last_tick
        if isinstance(raw_last_tick, int) and not isinstance(raw_last_tick, bool)
        else None
    )
    age = None if last_tick is None else max(0, timestamp - last_tick)
    external = next(
        (check for check in report.checks if check.name == "external_channel"),
        None,
    )
    cron_fresh = bool(age is not None and 0 <= age <= CRON_FRESH_SECONDS)
    return {
        "ok": report.ok,
        "checks": [asdict(check) for check in report.checks],
        "delivery_target_configured": bool(external is not None and external.ok),
        "last_tick": None if last_tick is None else _rfc3339(last_tick),
        "last_tick_age_seconds": age,
        "cron_appears_running": cron_fresh,
        "cron_freshness_seconds": CRON_FRESH_SECONDS,
        "in_process_ticker_enabled": settings.in_process_ticker_enabled,
        "ticker_interval_seconds": settings.ticker_interval_seconds,
        "cron_command": cron_command(),
        "tick_script": tick_script(),
        "job_name": JOB_NAME,
        "schedule": SCHEDULE,
    }
