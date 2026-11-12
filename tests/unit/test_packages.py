"""Smoke tests that every scaffold package imports."""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "hermes_calendar",
    "hermes_calendar.db",
    "hermes_calendar.hermes",
    "hermes_calendar.models",
    "hermes_calendar.recurrence",
    "hermes_calendar.reminders",
    "hermes_calendar.sync",
    "hermes_calendar.ui",
]


@pytest.mark.parametrize("package", PACKAGES)
def test_package_imports(package: str) -> None:
    assert importlib.import_module(package).__doc__
