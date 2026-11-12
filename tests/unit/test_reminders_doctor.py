"""Headless doctor: external channel and global-lease diagnostics."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from hermes_calendar import cli
from hermes_calendar.db.reminders import ReminderStore
from hermes_calendar.db.store import open_global_store, open_profile_store
from hermes_calendar.reminders import doctor


def _open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "alice"))
    return open_profile_store()


def test_doctor_fails_without_an_external_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path, monkeypatch)
    try:
        report = doctor.run_doctor(connection, which=lambda _: "/bin/hermes")
        assert report.ok is False
        external = next(check for check in report.checks if check.name == "external_channel")
        assert external.ok is False
        assert "in-app only" in external.detail
    finally:
        connection.close()


def test_doctor_passes_with_an_explicit_target_and_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path, monkeypatch)
    try:
        ReminderStore(connection).set_channel(
            "phone", enabled=True, config={"target": "matrix:@me"}
        )
        report = doctor.run_doctor(connection, which=lambda _: "/bin/hermes")
        assert report.ok is True
        assert next(
            check for check in report.checks if check.name == "global_lease"
        ).detail.startswith("global store unavailable")
    finally:
        connection.close()


def test_doctor_fails_when_the_enabled_channel_has_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path, monkeypatch)
    try:
        ReminderStore(connection).set_channel("phone", enabled=True, config={})
        report = doctor.run_doctor(connection, which=lambda _: "/bin/hermes")
        external = next(check for check in report.checks if check.name == "external_channel")
        assert external.ok is False
        assert "no explicit target" in external.detail
    finally:
        connection.close()


def test_doctor_fails_when_the_binary_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path, monkeypatch)
    try:
        ReminderStore(connection).set_channel(
            "phone", enabled=True, config={"target": "matrix:@me"}
        )
        report = doctor.run_doctor(connection, which=lambda _: None, binary="/missing")
        assert report.ok is False
        binary = next(check for check in report.checks if check.name == "hermes_binary")
        assert binary.ok is False
    finally:
        connection.close()


def test_doctor_reports_a_held_lease_and_a_broken_lease_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path, monkeypatch)
    global_connection = open_global_store()
    try:
        ReminderStore(connection).set_channel(
            "phone", enabled=True, config={"target": "matrix:@me"}
        )
        from hermes_calendar.db import coordination

        coordination.acquire_or_renew_lease(
            global_connection, profile_id="alice", now=1_800_000_000
        )
        report = doctor.run_doctor(
            connection, global_connection=global_connection, which=lambda _: "/bin/hermes"
        )
        lease = next(check for check in report.checks if check.name == "global_lease")
        assert lease.detail == "held by profile alice"

        global_connection.execute("DROP TABLE reminder_lease")
        report = doctor.run_doctor(
            connection, global_connection=global_connection, which=lambda _: "/bin/hermes"
        )
        lease = next(check for check in report.checks if check.name == "global_lease")
        assert lease.ok is False
    finally:
        global_connection.close()
        connection.close()


def test_cli_doctor_human_and_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _open(tmp_path, monkeypatch)
    connection.close()
    assert cli.main(["reminders", "doctor"]) == 1
    output = capsys.readouterr().out
    assert "FAIL: external_channel" in output

    connection = open_profile_store()
    try:
        ReminderStore(connection).set_channel(
            "phone", enabled=True, config={"target": "matrix:@me"}
        )
    finally:
        connection.close()
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/hermes")

    assert cli.main(["reminders", "doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["checks"]


def test_cli_doctor_degrades_when_the_global_store_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _open(tmp_path, monkeypatch)
    try:
        ReminderStore(connection).set_channel(
            "phone", enabled=True, config={"target": "matrix:@me"}
        )
    finally:
        connection.close()
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/hermes")

    def failing_global() -> None:
        raise sqlite3.OperationalError("global locked")

    monkeypatch.setattr(cli, "open_global_store", failing_global)
    assert cli.main(["reminders", "doctor"]) == 0
    assert "global store unavailable" in capsys.readouterr().out


def test_cli_doctor_reports_open_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_open() -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(cli, "open_profile_store", failing_open)
    assert cli.main(["reminders", "doctor"]) == 1
    assert "locked" in capsys.readouterr().err
