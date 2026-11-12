"""Tests for recurrence value objects and key helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from hermes_calendar.recurrence import (
    MasterEvent,
    Occurrence,
    OverrideEvent,
    all_day_recurrence_key,
    timed_recurrence_key,
)


def test_timed_recurrence_key_uses_epoch_seconds() -> None:
    instant = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    assert timed_recurrence_key(instant) == "T:1772463600"


def test_all_day_recurrence_key_uses_iso_date() -> None:
    assert all_day_recurrence_key(date(2026, 3, 2)) == "D:2026-03-02"


def test_master_event_timed_properties() -> None:
    master = MasterEvent(
        start_local=datetime(2026, 3, 2, 9, 0),
        start_tz="America/Chicago",
        duration_seconds=3600,
        rrule="FREQ=DAILY",
    )

    assert master.is_all_day is False


def test_master_event_all_day_properties() -> None:
    master = MasterEvent(start_date=date(2026, 3, 2), end_date=date(2026, 3, 4))

    assert master.is_all_day is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start_date": date(2026, 3, 2)}, "both start_date and end_date"),
        (
            {
                "start_date": date(2026, 3, 2),
                "end_date": date(2026, 3, 4),
                "start_local": datetime(2026, 3, 2, 9),
            },
            "both all-day and timed",
        ),
        (
            {
                "start_date": date(2026, 3, 2),
                "end_date": date(2026, 3, 4),
                "duration_seconds": 60,
            },
            "use end_date",
        ),
        (
            {"start_date": date(2026, 3, 4), "end_date": date(2026, 3, 4)},
            "must be after start_date",
        ),
        ({}, "require start_local"),
        (
            {"start_local": datetime(2026, 3, 2, 9, tzinfo=UTC), "duration_seconds": 1},
            "naive wall-clock",
        ),
        ({"start_local": datetime(2026, 3, 2, 9)}, "require duration_seconds"),
        (
            {"start_local": datetime(2026, 3, 2, 9), "duration_seconds": -1},
            "must not be negative",
        ),
        (
            {
                "start_local": datetime(2026, 3, 2, 9),
                "start_tz": "  ",
                "duration_seconds": 1,
            },
            "non-empty IANA",
        ),
    ],
)
def test_master_event_rejects_incoherent_timing(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MasterEvent(**kwargs)  # type: ignore[arg-type]


def test_master_event_rejects_blank_rule() -> None:
    with pytest.raises(ValueError, match="rrule must be non-empty"):
        MasterEvent(
            start_local=datetime(2026, 3, 2, 9),
            duration_seconds=60,
            rrule="  ",
        )


def test_override_event_normalizes_status_and_reports_properties() -> None:
    override = OverrideEvent(
        recurrence_key="T:1",
        start_local=datetime(2026, 3, 2, 14),
        duration_seconds=1800,
        status="tentative",
    )

    assert override.status == "TENTATIVE"
    assert override.is_cancelled is False
    assert override.is_all_day is False


def test_override_event_cancelled_and_all_day() -> None:
    override = OverrideEvent(
        recurrence_key="D:2026-03-02",
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        status="cancelled",
    )

    assert override.is_cancelled is True
    assert override.is_all_day is True


def test_override_event_rejects_blank_key() -> None:
    with pytest.raises(ValueError, match="recurrence_key must be non-empty"):
        OverrideEvent(
            recurrence_key="  ",
            start_local=datetime(2026, 3, 2, 14),
            duration_seconds=1800,
        )


def test_override_event_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown override status"):
        OverrideEvent(
            recurrence_key="T:1",
            start_local=datetime(2026, 3, 2, 14),
            duration_seconds=1800,
            status="REJECTED",
        )


def test_occurrence_override_property() -> None:
    override = OverrideEvent(
        recurrence_key="T:1",
        start_local=datetime(2026, 3, 2, 14),
        duration_seconds=1800,
    )
    occurrence = Occurrence(
        recurrence_key="T:1",
        start_utc=datetime(2026, 3, 2, 20, tzinfo=UTC),
        end_utc=datetime(2026, 3, 2, 20, 30, tzinfo=UTC),
        is_all_day=False,
        start_local=datetime(2026, 3, 2, 14),
        end_local=datetime(2026, 3, 2, 14, 30),
        override=override,
    )

    assert occurrence.is_override is True
    assert occurrence.override is override
