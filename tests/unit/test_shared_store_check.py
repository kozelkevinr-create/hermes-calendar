"""Tests for the generic shared-store sentinel check.

The module is copied verbatim into other plugin repositories, so these tests use
a neutral plugin name and drive the storage root through ``HERMES_HOME``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_calendar import shared_store_check
from hermes_calendar.plugin import calendar_status
from hermes_calendar.plugin_storage import global_dir, open_db
from hermes_calendar.shared_store_check import SharedStoreStatus, verify_shared

PLUGIN = "demo-plugin"
DB_NAME = "_shared_check.db"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point profile and global resolution at a fresh temporary Hermes root."""
    profile_home = tmp_path / "root" / "profiles" / "one"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    return profile_home


def check(profile_id: str, **kwargs: Any) -> SharedStoreStatus:
    """Run the probe for the test plugin."""
    return verify_shared(PLUGIN, profile_id, **kwargs)


def global_db_path() -> Path:
    """Return the sentinel database path for the test plugin."""
    return global_dir(PLUGIN) / DB_NAME


class _Proxy:
    """Minimal ``sqlite3.Connection`` stand-in used to inject failures."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._real.execute(sql, parameters)

    def close(self) -> None:
        self._real.close()


class _SelectFails(_Proxy):
    """Let registration succeed, then fail every read."""

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        if sql.lstrip().upper().startswith("SELECT"):
            raise sqlite3.OperationalError("disk I/O error")
        return super().execute(sql, parameters)


class _CloseFails(_Proxy):
    """Fail after the real connection closed, to exercise cleanup suppression."""

    def close(self) -> None:
        super().close()
        raise sqlite3.OperationalError("close failed")


class _NoJournalRow:
    """Cursor stand-in whose ``fetchone`` returns nothing."""

    def fetchone(self) -> None:
        return None


class _FakeTime:
    """Deterministic clock/sleep pair for exercising the retry budget."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _JournalModeUnknown(_Proxy):
    """Pretend the journal-mode pragma returned no row."""

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        if "journal_mode" in sql.lower():
            return cast(sqlite3.Cursor, _NoJournalRow())
        return super().execute(sql, parameters)


def _wrap(monkeypatch: pytest.MonkeyPatch, wrapper: type[_Proxy]) -> None:
    """Route the probe's ``open_db`` through a failure-injecting proxy."""

    def open_proxy(path: Path) -> sqlite3.Connection:
        return cast(sqlite3.Connection, wrapper(open_db(path)))

    monkeypatch.setattr(shared_store_check, "open_db", open_proxy)


# ------------------------------------------------------------------ basic answer


def test_single_profile_is_writable_but_sharing_is_unknown(home: Path) -> None:
    del home
    status = check("one")

    assert status.ok is True
    assert status.writable is True
    assert status.shared is None
    assert status.peers_seen == ()
    assert "only this profile has registered" in status.detail
    assert global_db_path().is_file()


def test_second_profile_sees_the_first(home: Path) -> None:
    del home
    first = check("one")
    second = check("two")

    assert first.shared is None
    assert second.ok is True
    assert second.shared is True
    assert second.peers_seen == ("one",)
    assert "one" in second.detail

    again = check("one")
    assert again.shared is True
    assert again.peers_seen == ("two",)


@pytest.mark.parametrize("profile_id", ["A1_b-2", "a" * 64])
def test_valid_profile_ids_are_accepted(home: Path, profile_id: str) -> None:
    del home
    status = check(profile_id)

    assert status.ok is True
    assert status.writable is True


def test_stale_peer_is_ignored(home: Path) -> None:
    del home
    base = 1_000_000.0
    peer = verify_shared(PLUGIN, "peer", clock=lambda: base)
    assert peer.ok is True

    fresh = verify_shared(PLUGIN, "me", stale_after_s=30.0, clock=lambda: base + 10.0)
    assert fresh.shared is True
    assert fresh.peers_seen == ("peer",)

    stale = verify_shared(PLUGIN, "me", stale_after_s=30.0, clock=lambda: base + 60.0)
    assert stale.ok is True
    assert stale.writable is True
    assert stale.shared is False
    assert stale.peers_seen == ()
    assert "stale" in stale.detail


# ---------------------------------------------------------------- failure modes


def test_read_only_directory_is_reported(home: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    directory = global_dir(PLUGIN)
    directory.chmod(0o500)
    try:
        status = check("one")
    finally:
        directory.chmod(0o700)

    assert status.ok is False
    assert status.writable is False
    assert status.shared is None
    assert "not writable" in status.detail


def test_permission_error_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home

    def deny(path: Path) -> sqlite3.Connection:
        raise PermissionError("permission denied")

    monkeypatch.setattr(shared_store_check, "open_db", deny)

    status = check("one")

    assert status.ok is False
    assert status.writable is False
    assert "permission denied" in status.detail


def test_corrupt_database_is_reported(home: Path) -> None:
    del home
    global_db_path().parent.mkdir(parents=True, exist_ok=True)
    global_db_path().write_bytes(b"definitely not a database" * 8)

    status = check("one")

    assert status.ok is False
    assert status.writable is False
    assert "corrupt" in status.detail


def test_locked_database_exhausts_retry_budget(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home
    fake = _FakeTime()

    def locked(path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(shared_store_check, "open_db", locked)

    status = check("one", sleep=fake.sleep, clock=fake.clock)

    assert status.ok is False
    assert status.writable is False
    assert "locked" in status.detail
    assert fake.sleeps
    assert sum(fake.sleeps) <= shared_store_check._RETRY_BUDGET_S + 1e-9


@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_transient_lock_is_retried_until_success(
    home: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    del home
    remaining_failures = 3
    sleeps: list[float] = []

    def locked_then_open(path: Path) -> sqlite3.Connection:
        nonlocal remaining_failures
        if remaining_failures:
            remaining_failures -= 1
            raise sqlite3.OperationalError(message)
        return open_db(path)

    monkeypatch.setattr(shared_store_check, "open_db", locked_then_open)

    status = check("one", sleep=sleeps.append, clock=lambda: 0.0)

    assert status.ok is True
    assert status.writable is True
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps)


def test_registration_lock_is_retried_after_closing_connection(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del home
    remaining_lock = 1
    sleeps: list[float] = []

    class _RegisterLocked(_Proxy):
        """Raise a transient lock on the first ``BEGIN IMMEDIATE``."""

        def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
            nonlocal remaining_lock
            if sql == "BEGIN IMMEDIATE" and remaining_lock:
                remaining_lock -= 1
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, parameters)

    def open_proxy(path: Path) -> sqlite3.Connection:
        return cast(sqlite3.Connection, _RegisterLocked(open_db(path)))

    monkeypatch.setattr(shared_store_check, "open_db", open_proxy)

    status = check("one", sleep=sleeps.append, clock=lambda: 0.0)

    assert status.ok is True
    assert status.writable is True
    assert len(sleeps) == 1


def test_non_lock_operational_error_is_not_retried(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del home
    sleeps: list[float] = []

    def failing(path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("no such table: shared_sentinel")

    monkeypatch.setattr(shared_store_check, "open_db", failing)

    status = check("one", sleep=sleeps.append)

    assert status.ok is False
    assert sleeps == []
    assert "unreadable or corrupt" in status.detail


def test_io_error_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home

    def failing(path: Path) -> sqlite3.Connection:
        raise OSError("input/output error")

    monkeypatch.setattr(shared_store_check, "open_db", failing)

    status = check("one")

    assert status.ok is False
    assert "not accessible" in status.detail


def test_unexpected_error_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home

    def failing(path: Path) -> sqlite3.Connection:
        raise RuntimeError("boom")

    monkeypatch.setattr(shared_store_check, "open_db", failing)

    status = check("one")

    assert status.ok is False
    assert status.detail == "shared store check failed: RuntimeError: boom"


def test_unavailable_directory_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home

    def unavailable(name: str) -> Path:
        raise OSError("no such file or directory")

    monkeypatch.setattr(shared_store_check, "global_dir", unavailable)

    status = check("one")

    assert status.ok is False
    assert status.writable is False
    assert "directory is unavailable" in status.detail


@pytest.mark.parametrize(
    "profile_id",
    ["", ".", "..", "a/b", "a\\b", "a.b", "a b", "-leading", "a" * 65, "caf\u00e9"],
)
def test_invalid_profile_ids_are_rejected_without_raising(home: Path, profile_id: str) -> None:
    status = check(profile_id)

    assert status.ok is False
    assert status.shared is None
    assert status.writable is False
    assert "invalid profile id" in status.detail


@pytest.mark.parametrize("plugin_name", ["", ".", "..", "a/b", "a.b", "a" * 65])
def test_invalid_plugin_names_are_rejected_without_raising(home: Path, plugin_name: str) -> None:
    status = verify_shared(plugin_name, "one")

    assert status.ok is False
    assert "invalid plugin name" in status.detail


def test_non_string_identifiers_are_rejected_without_raising(home: Path) -> None:
    del home
    bad_plugin = verify_shared(cast(str, 5), "one")
    bad_profile = verify_shared(PLUGIN, cast(str, None))

    assert bad_plugin.ok is False
    assert "invalid plugin name" in bad_plugin.detail
    assert bad_profile.ok is False
    assert "invalid profile id" in bad_profile.detail


def test_non_numeric_arguments_are_rejected_without_raising(home: Path) -> None:
    del home
    status = check("one", wait_s=cast(float, "soon"))

    assert status.ok is False
    assert "must be numbers" in status.detail


# ------------------------------------------------------------------- filesystem


def test_non_wal_journal_mode_warns(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home

    def downgrade(path: Path) -> sqlite3.Connection:
        connection = open_db(path)
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    monkeypatch.setattr(shared_store_check, "open_db", downgrade)

    status = check("one")

    assert status.ok is True
    assert status.writable is True
    assert "locking may be unreliable" in status.detail
    assert "'delete'" in status.detail


def test_unknown_journal_mode_warns(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home
    _wrap(monkeypatch, _JournalModeUnknown)

    status = check("one")

    assert status.ok is True
    assert "journal mode is 'unknown'" in status.detail


def test_read_failure_after_write_keeps_writable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del home
    _wrap(monkeypatch, _SelectFails)

    status = check("one")

    assert status.ok is False
    assert status.writable is True
    assert status.shared is None
    assert "unreadable or corrupt" in status.detail


def test_failing_close_does_not_hide_success(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home
    _wrap(monkeypatch, _CloseFails)

    status = check("one")

    assert status.ok is True
    assert status.writable is True


# ---------------------------------------------------------------------- waiting


def test_wait_returns_after_budget_without_peer(home: Path) -> None:
    del home
    started = time.monotonic()

    status = check("one", wait_s=0.05)

    assert time.monotonic() - started >= 0.04
    assert status.ok is True
    assert status.shared is None


def test_wait_sees_a_concurrent_registration(home: Path) -> None:
    del home
    release = threading.Event()
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        if calls >= 2:
            release.set()
        return time.time()

    def late_profile() -> SharedStoreStatus:
        assert release.wait(5.0)
        return check("late")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(late_profile)
        status = check("early", wait_s=5.0, clock=clock)
        peer = future.result()

    assert peer.ok is True
    assert status.ok is True
    assert status.shared is True
    assert status.peers_seen == ("late",)


def test_concurrent_profiles_never_raise(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    del home
    profile_ids = [f"worker-{index}" for index in range(4)]
    failures: list[str] = []
    passed = 0

    for iteration in range(50):
        monkeypatch.setenv(
            "HERMES_HOME",
            str(tmp_path / f"run-{iteration}" / "profiles" / "one"),
        )
        barrier = threading.Barrier(len(profile_ids))

        def probe(profile_id: str, barrier: threading.Barrier = barrier) -> SharedStoreStatus:
            barrier.wait(timeout=10.0)
            return check(profile_id, wait_s=2.0, sleep=lambda _seconds: None)

        with ThreadPoolExecutor(max_workers=len(profile_ids)) as pool:
            results = list(pool.map(probe, profile_ids))

        iteration_failures: list[str] = []
        for profile_id, result in zip(profile_ids, results, strict=True):
            if not result.ok or not result.writable:
                iteration_failures.append(
                    f"iteration {iteration}, {profile_id}: ok={result.ok} "
                    f"writable={result.writable}: {result.detail}"
                )
            if profile_id in result.peers_seen:
                iteration_failures.append(
                    f"iteration {iteration}, {profile_id}: saw its own sentinel"
                )
        if not any(result.shared is True for result in results):
            iteration_failures.append(f"iteration {iteration}: no profile saw a peer")

        if iteration_failures:
            failures.extend(iteration_failures)
        else:
            passed += 1

    assert passed == 50, failures


# ------------------------------------------------------------------- integration


def test_calendar_status_exposes_shared_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / "work"
    monkeypatch.setenv("HERMES_HOME", str(home))

    payload: dict[str, Any] = json.loads(calendar_status())

    shared = payload["shared_store"]
    assert shared["ok"] is True
    assert shared["shared"] is None
    assert shared["peers_seen"] == []
    assert shared["writable"] is True
    assert isinstance(shared["detail"], str)


def test_module_is_generic() -> None:
    source = Path(shared_store_check.__file__).read_text(encoding="utf-8").lower()

    assert "calendar" not in source
    assert "hermes_calendar" not in source
    assert "from .plugin_storage import global_dir, open_db" in source
