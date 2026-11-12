"""Tests for the CLI entry point."""

from __future__ import annotations

import pytest

from hermes_calendar import __version__
from hermes_calendar.cli import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"hermes-calendar {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: hermes-calendar" in capsys.readouterr().out


def test_unknown_argument_exits() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--not-a-flag"])
    assert excinfo.value.code == 2
