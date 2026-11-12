"""Tests for the generic per-plugin storage helper.

The module is copied verbatim into other plugin repositories, so these tests
must not rely on the calendar package or on a Hermes install.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_calendar import plugin_storage
from hermes_calendar.plugin_storage import global_dir, open_db, profile_dir

PLUGIN = "hermes-calendar"


def install_fake_constants(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: list[Path],
    root: list[Path],
) -> None:
    """Install a fake ``hermes_constants`` whose answers can move per call."""
    module: Any = ModuleType("hermes_constants")

    def next_home() -> Path:
        return home.pop(0)

    def next_root() -> Path:
        return root.pop(0)

    module.get_hermes_home = next_home
    module.get_default_hermes_root = next_root
    monkeypatch.setitem(sys.modules, "hermes_constants", module)


def test_profile_dir_follows_env_profile_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_home = tmp_path / "profiles" / "one"
    monkeypatch.setenv("HERMES_HOME", str(first_home))

    first = profile_dir(PLUGIN)

    assert first == first_home / "plugin-data" / PLUGIN
    assert first.is_dir()

    second_home = tmp_path / "profiles" / "two"
    monkeypatch.setenv("HERMES_HOME", str(second_home))

    second = profile_dir(PLUGIN)

    assert second == second_home / "plugin-data" / PLUGIN
    assert second.is_dir()
    assert second != first


def test_profile_dir_defaults_to_home_hermes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    path = profile_dir(PLUGIN)

    assert path == tmp_path / ".hermes" / "plugin-data" / PLUGIN
    assert path.is_dir()


def test_global_dir_strips_profiles_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "work"))

    path = global_dir(PLUGIN)

    assert path == tmp_path / "root" / "plugin-data" / PLUGIN
    assert path.is_dir()


def test_global_dir_uses_custom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv("HERMES_HOME", str(custom_root))

    assert global_dir(PLUGIN) == custom_root / "plugin-data" / PLUGIN


def test_paths_are_resolved_from_constants_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    homes = [tmp_path / "home-one", tmp_path / "home-two"]
    roots = [tmp_path / "root-one", tmp_path / "root-two"]
    install_fake_constants(monkeypatch, home=homes, root=roots)

    assert profile_dir(PLUGIN) == tmp_path / "home-one" / "plugin-data" / PLUGIN
    assert profile_dir(PLUGIN) == tmp_path / "home-two" / "plugin-data" / PLUGIN
    assert global_dir(PLUGIN) == tmp_path / "root-one" / "plugin-data" / PLUGIN
    assert global_dir(PLUGIN) == tmp_path / "root-two" / "plugin-data" / PLUGIN


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "a\\b", "a.b", "a b", "-leading", "a" * 65, "caf\u00e9"],
)
def test_invalid_names_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="invalid plugin name"):
        profile_dir(name)
    with pytest.raises(ValueError, match="invalid plugin name"):
        global_dir(name)


@pytest.mark.parametrize("name", ["a", "A1_b-2", "a" * 64])
def test_valid_names_are_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert profile_dir(name).name == name
    assert global_dir(name).name == name


def test_open_db_configures_file_database(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.db"

    connection = open_db(path)

    try:
        assert path.is_file()
        assert connection.row_factory is sqlite3.Row
        assert connection.isolation_level is None
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    finally:
        connection.close()


def test_open_db_skips_wal_for_memory_database() -> None:
    connection = open_db(Path(":memory:"))

    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_open_db_rows_are_addressable_by_name(tmp_path: Path) -> None:
    connection = open_db(tmp_path / "data.db")
    try:
        connection.execute("CREATE TABLE t (value TEXT)")
        connection.execute("INSERT INTO t (value) VALUES (?)", ("x",))

        row = connection.execute("SELECT value FROM t").fetchone()

        assert row is not None
        assert row["value"] == "x"
    finally:
        connection.close()


def test_two_connections_write_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    setup = open_db(path)
    setup.execute("CREATE TABLE writes (id INTEGER PRIMARY KEY, worker TEXT)")
    setup.close()

    def writer(worker: str) -> None:
        connection = open_db(path)
        try:
            for _ in range(10):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO writes (worker) VALUES (?)", (worker,))
                connection.execute("COMMIT")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, worker) for worker in ("one", "two")]
        for future in futures:
            future.result()

    check = open_db(path)
    try:
        assert check.execute("SELECT count(*) FROM writes").fetchone()[0] == 20
    finally:
        check.close()


def test_connection_can_be_used_from_another_thread(tmp_path: Path) -> None:
    connection = open_db(tmp_path / "data.db")
    try:
        connection.execute("CREATE TABLE t (value TEXT)")

        thread = threading.Thread(
            target=lambda: connection.execute("INSERT INTO t (value) VALUES (?)", ("x",))
        )
        thread.start()
        thread.join()

        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        connection.close()


def test_open_db_closes_connection_when_configuration_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failing = MagicMock(spec=sqlite3.Connection)
    failing.execute.side_effect = sqlite3.OperationalError("boom")
    monkeypatch.setattr(sqlite3, "connect", MagicMock(return_value=failing))

    with pytest.raises(sqlite3.OperationalError, match="boom"):
        open_db(tmp_path / "data.db")

    failing.close.assert_called_once()


def test_storage_module_is_generic() -> None:
    source = Path(plugin_storage.__file__).read_text(encoding="utf-8").lower()

    assert "calendar" not in source
