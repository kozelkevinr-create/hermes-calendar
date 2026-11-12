"""In-process ticker gating and CLI entry-point tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from hermes_calendar import cli
from hermes_calendar.db.reminders import ReminderStore
from hermes_calendar.db.store import open_profile_store
from hermes_calendar.reminders import ticker as ticker_module
from hermes_calendar.reminders.cron import cron_command, tick_script
from hermes_calendar.reminders.engine import ProfileMismatchError
from hermes_calendar.reminders.ticker import (
    InProcessTicker,
    maybe_start_in_process_ticker,
)
from tests.unit.reminder_support import create_calendar, create_timed_event


class FakeContext:
    def __init__(self, *, with_spawn: bool = True) -> None:
        self.tasks: list[Coroutine[Any, Any, None]] = []
        if with_spawn:
            self.spawn_task = self._spawn

    def _spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> object:
        self.tasks.append(coro)
        return object()


def test_ticker_runs_ticks_and_stops() -> None:
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)

    async def scenario() -> None:
        instance = InProcessTicker(tick=tick, interval_seconds=0.01)
        task = asyncio.create_task(instance.run())
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        instance.stop()
        await task

    asyncio.run(scenario())
    assert len(calls) >= 2


def test_ticker_that_is_stopped_before_running_does_nothing() -> None:
    calls: list[int] = []
    instance = InProcessTicker(tick=lambda: calls.append(1), interval_seconds=0.01)
    instance.stop()
    asyncio.run(instance.run())
    assert calls == []


def test_ticker_cancellation_sets_stopped() -> None:
    async def scenario() -> None:
        instance = InProcessTicker(tick=lambda: None, interval_seconds=60)
        task = asyncio.create_task(instance.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert instance._stopped.is_set()

    asyncio.run(scenario())


def test_maybe_start_requires_spawn_task() -> None:
    assert maybe_start_in_process_ticker(FakeContext(with_spawn=False)) is None


def test_maybe_start_is_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    assert maybe_start_in_process_ticker(FakeContext()) is None


def test_maybe_start_starts_only_when_explicitly_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    connection = open_profile_store()
    try:
        ReminderStore(connection).set_pref(
            "notifications",
            {"in_process_ticker": {"enabled": True, "interval_seconds": 5}},
        )
    finally:
        connection.close()

    ctx = FakeContext()
    instance = maybe_start_in_process_ticker(ctx)
    try:
        assert instance is not None
        assert instance.interval_seconds == 5
        assert len(ctx.tasks) == 1
    finally:
        if instance is not None:
            instance.stop()
        for coro in ctx.tasks:
            coro.close()


def test_maybe_start_fails_closed_when_the_store_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))

    def failing_open() -> sqlite3.Connection:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(ticker_module, "open_profile_store", failing_open)
    assert maybe_start_in_process_ticker(FakeContext()) is None


def test_maybe_start_fails_closed_when_the_pref_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))

    def failing_enabled(connection: sqlite3.Connection) -> tuple[bool, int]:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(ticker_module, "ticker_enabled", failing_enabled)
    assert maybe_start_in_process_ticker(FakeContext()) is None


def test_cron_command_is_exact() -> None:
    assert cron_command() == (
        'hermes cron create --name "calendar reminders" --no-agent '
        '--script hermes-calendar-tick.sh --deliver local --catch-up on "1m"'
    )
    assert "hermes_calendar reminders tick --quiet" in tick_script()
    assert tick_script().startswith("#!/bin/sh")


def test_cli_cron_command_prints_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["reminders", "cron-command"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == cron_command()
    assert "hermes_calendar reminders tick --quiet" in captured.err


def test_cli_tick_quiet_succeeds_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    assert cli.main(["reminders", "tick", "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_tick_json_and_human_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    connection = open_profile_store()
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=2_000_000_000)
    finally:
        connection.close()

    assert cli.main(["reminders", "tick", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile_id"] == "alice"

    assert cli.main(["reminders", "tick"]) == 0
    assert capsys.readouterr().out.startswith("reminders: claimed=")


def test_cli_prints_a_global_coordination_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))

    def degraded() -> object:
        from hermes_calendar.reminders.models import TickResult

        return TickResult(profile_id="alice", global_error="global unavailable")

    monkeypatch.setattr(cli, "run_tick", degraded)
    assert cli.main(["reminders", "tick"]) == 0
    assert "global unavailable" in capsys.readouterr().err


def test_cli_tick_reports_profile_and_store_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def mismatch() -> None:
        raise ProfileMismatchError("wrong profile")

    monkeypatch.setattr(cli, "run_tick", mismatch)
    assert cli.main(["reminders", "tick"]) == 1
    assert "wrong profile" in capsys.readouterr().err

    def broken() -> None:
        raise sqlite3.OperationalError("database is busy")

    monkeypatch.setattr(cli, "run_tick", broken)
    assert cli.main(["reminders", "tick"]) == 1
    assert "database is busy" in capsys.readouterr().err


def test_cli_reminders_without_a_subcommand_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["reminders"]) == 0
    assert "usage: hermes-calendar" in capsys.readouterr().out
