"""Tests for the active profile identity resolver."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hermes_calendar.profile_identity import (
    ProfileIdentityError,
    current_profile_id,
    validate_profile_id,
)


class _Ctx:
    """Minimal stand-in carrying the public ``profile_name`` attribute."""

    def __init__(self, profile_name: Any) -> None:
        self.profile_name = profile_name


def _install_constants(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: Path | list[Path],
    root: Path | list[Path],
) -> None:
    """Install a fake ``hermes_constants`` returning fixed or queued paths."""
    homes = list(home) if isinstance(home, list) else None
    roots = list(root) if isinstance(root, list) else None
    module: Any = ModuleType("hermes_constants")
    module.get_hermes_home = (lambda: homes.pop(0)) if homes is not None else (lambda: home)
    module.get_default_hermes_root = (lambda: roots.pop(0)) if roots is not None else (lambda: root)
    monkeypatch.setitem(sys.modules, "hermes_constants", module)


def test_default_home_resolves_to_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert current_profile_id() == "default"


def test_default_root_resolves_to_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "docker-root"
    monkeypatch.setenv("HERMES_HOME", str(root))

    assert current_profile_id() == "default"


def test_named_profile_resolves_to_its_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "coding-style"))

    assert current_profile_id() == "coding-style"


def test_custom_home_resolves_to_custom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_constants(
        monkeypatch,
        home=tmp_path / "somewhere" / "else",
        root=tmp_path / "hermes-root",
    )

    assert current_profile_id() == "custom"


def test_identity_is_resolved_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_constants(
        monkeypatch,
        home=[
            tmp_path / "root" / "profiles" / "one",
            tmp_path / "root" / "profiles" / "two",
        ],
        root=[tmp_path / "root", tmp_path / "root"],
    )

    assert current_profile_id() == "one"
    assert current_profile_id() == "two"


def test_valid_ctx_profile_name_is_preferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "derived"))

    assert current_profile_id(_Ctx("coding-style")) == "coding-style"


@pytest.mark.parametrize("candidate", [".hermes", "a.b", "-leading", "", 7, None, b"bytes"])
def test_invalid_or_non_string_ctx_profile_name_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate: Any
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "derived"))

    assert current_profile_id(_Ctx(candidate)) == "derived"


def test_missing_ctx_attribute_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "derived"))

    assert current_profile_id(object()) == "derived"


def test_invalid_derived_id_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / ".hermes"))

    with pytest.raises(ProfileIdentityError, match="invalid profile id"):
        current_profile_id()


def test_profile_identity_error_is_a_value_error() -> None:
    assert issubclass(ProfileIdentityError, ValueError)


def test_validate_profile_id_accepts_valid_ids() -> None:
    assert validate_profile_id("default") == "default"
    assert validate_profile_id("custom") == "custom"
    assert validate_profile_id("a") == "a"
    assert validate_profile_id("A0_-") == "A0_-"
    assert validate_profile_id("a" * 64) == "a" * 64


@pytest.mark.parametrize("value", ["", ".hermes", "a.b", "a b", "a/b", "-x", "a" * 65])
def test_validate_profile_id_rejects_invalid_ids(value: str) -> None:
    with pytest.raises(ProfileIdentityError, match="invalid profile id"):
        validate_profile_id(value)


def test_validate_profile_id_rejects_non_strings() -> None:
    with pytest.raises(ProfileIdentityError, match="invalid profile id"):
        validate_profile_id(42)  # type: ignore[arg-type]
