"""Shared fixtures for reminder tests: real SQLite stores, fake clock/runner."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hermes_calendar.db import connect, migrate
from hermes_calendar.db.reminders import ReminderStore
from hermes_calendar.models import Calendar, Event, EventDraft, Reminder
from hermes_calendar.reminders.adapters import CommandRunner, build_chat_notifier
from hermes_calendar.reminders.engine import ReminderEngine
from hermes_calendar.reminders.models import NotificationSettings

LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"


@dataclass
class FakeClock:
    """Deterministic epoch-seconds clock."""

    now: int

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds


@dataclass
class FakeRunner:
    """A ``hermes send`` stand-in that records argv and returns canned output."""

    returncode: int = 0
    stdout: str = '{"message_id": "msg-1"}'
    stderr: str = ""
    timeout: bool = False
    calls: list[list[str]] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.bodies.append(input_text)
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def profile_store(tmp_path: Path, name: str = "alice") -> sqlite3.Connection:
    """Open a migrated profile store for ``name``."""
    connection = connect(tmp_path / f"{name}.db")
    migrate(
        connection,
        store_scope="profile",
        store_id=f"profile:{name}",
        owner_profile_id=name,
    )
    return connection


def global_store(tmp_path: Path, name: str = "global") -> sqlite3.Connection:
    """Open a migrated shared global store."""
    connection = connect(tmp_path / f"{name}.db")
    migrate(connection, store_scope="global", store_id="global", owner_profile_id="")
    return connection


def create_calendar(connection: sqlite3.Connection, *, tz: str = "UTC") -> Calendar:
    from hermes_calendar.db import Repository

    return Repository(connection).create_calendar("Work", default_tz=tz)


def create_timed_event(
    connection: sqlite3.Connection,
    calendar: Calendar,
    *,
    start_utc: int,
    duration_seconds: int = 3600,
    summary: str = "Dentist",
    reminders: tuple[Reminder, ...] = (Reminder(offset_seconds=-600),),
    rrule: str | None = None,
    start_tz: str | None = "UTC",
    status: str = "CONFIRMED",
    transparency: str = "OPAQUE",
    xprops: dict[str, Any] | None = None,
) -> Event:
    """Create one timed master event through the real repository."""
    from hermes_calendar.db import Repository

    zone = ZoneInfo(start_tz or "UTC")
    local = datetime.fromtimestamp(start_utc, zone)
    draft = EventDraft(
        calendar_id=calendar.id,
        summary=summary,
        start_utc=start_utc,
        end_utc=start_utc + duration_seconds,
        start_local=local.strftime(LOCAL_FORMAT),
        start_tz=start_tz,
        end_local=datetime.fromtimestamp(start_utc + duration_seconds, zone).strftime(LOCAL_FORMAT),
        end_tz=start_tz,
        duration_seconds=duration_seconds,
        rrule=rrule,
        status=status,
        transparency=transparency,
        xprops={} if xprops is None else xprops,
        reminders=reminders,
    )
    return Repository(connection).create_event(draft)


def create_all_day_event(
    connection: sqlite3.Connection,
    calendar: Calendar,
    *,
    day: date,
    days: int = 1,
    summary: str = "Holiday",
    reminders: tuple[Reminder, ...] = (Reminder(offset_seconds=-86_400),),
) -> Event:
    """Create one all-day master event."""
    from hermes_calendar.db import Repository

    end = date.fromordinal(day.toordinal() + days)
    draft = EventDraft(
        calendar_id=calendar.id,
        summary=summary,
        start_utc=int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp()),
        end_utc=int(datetime.combine(end, datetime.min.time(), tzinfo=UTC).timestamp()),
        start_date=day.isoformat(),
        end_date=end.isoformat(),
        is_all_day=True,
        reminders=reminders,
    )
    return Repository(connection).create_event(draft)


def enable_chat(
    connection: sqlite3.Connection, *, target: str = "matrix:@alice:example.org"
) -> None:
    """Explicitly enable the chat channel with an allowlisted target."""
    ReminderStore(connection).set_channel(
        "phone", enabled=True, config={"target": target, "subject_prefix": "Calendar"}
    )


def make_engine(
    profile_connection: sqlite3.Connection,
    *,
    profile_id: str = "alice",
    clock: FakeClock,
    runner: CommandRunner | None = None,
    global_connection: sqlite3.Connection | None = None,
    settings: NotificationSettings | None = None,
) -> ReminderEngine:
    """Build an engine with a fake clock, deterministic jitter, and fake runner."""
    chat = None
    if runner is not None:
        chat = build_chat_notifier(ReminderStore(profile_connection).channels(), runner=runner)
    return ReminderEngine(
        profile_connection=profile_connection,
        profile_id=profile_id,
        global_connection=global_connection,
        settings=settings or NotificationSettings(jitter_ratio=0.0),
        chat_notifier=chat,
        clock=clock,
        rng=lambda: 0.5,
    )
