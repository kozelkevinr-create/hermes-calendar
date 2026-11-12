"""Tests for model identifiers."""

from __future__ import annotations

import uuid

import pytest

from hermes_calendar.models import new_uuid7


def test_new_uuid7_has_version_and_variant() -> None:
    value = new_uuid7(1_700_000_000_000)

    parsed = uuid.UUID(value)

    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122
    assert parsed.int >> 80 == 1_700_000_000_000


def test_new_uuid7_is_time_sortable() -> None:
    first = new_uuid7(1_000)
    second = new_uuid7(2_000)

    assert first < second


def test_new_uuid7_defaults_to_the_clock() -> None:
    assert uuid.UUID(new_uuid7()).version == 7


@pytest.mark.parametrize("timestamp", [-1, 1 << 48])
def test_new_uuid7_rejects_out_of_range_timestamps(timestamp: int) -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        new_uuid7(timestamp)
