"""Tests for pure recurrence expansion."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta

import pytest

from hermes_calendar.recurrence import (
    ExpansionLimitExceeded,
    MasterEvent,
    Occurrence,
    OverrideEvent,
    UnsupportedRecurrence,
    expand,
)

expand_module = importlib.import_module("hermes_calendar.recurrence.expand")

CHICAGO = "America/Chicago"


def _local(item: Occurrence) -> datetime:
    assert item.start_local is not None
    return item.start_local


def _local_end(item: Occurrence) -> datetime:
    assert item.end_local is not None
    return item.end_local


def _local_date(item: Occurrence) -> date:
    return _local(item).date()


def _date_start(item: Occurrence) -> date:
    assert item.start_date is not None
    return item.start_date


def _date_end(item: Occurrence) -> date:
    assert item.end_date is not None
    return item.end_date


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _master(
    *,
    start: datetime | None = datetime(2026, 3, 1, 9, 0),
    tz: str | None = CHICAGO,
    duration: int | None = 3600,
    start_utc: datetime | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    rrule: str | None = None,
    rdate: tuple[str, ...] = (),
    exdate: tuple[str, ...] = (),
) -> MasterEvent:
    return MasterEvent(
        start_local=start,
        start_tz=tz,
        start_utc=start_utc,
        duration_seconds=duration,
        start_date=start_date,
        end_date=end_date,
        rrule=rrule,
        rdate=rdate,
        exdate=exdate,
    )


def test_non_recurring_event_is_returned_when_it_overlaps() -> None:
    master = _master()

    occurrences = expand(master, _utc(2026, 3, 1, 14), _utc(2026, 3, 1, 16))

    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence.start_utc == _utc(2026, 3, 1, 15)
    assert occurrence.end_utc == _utc(2026, 3, 1, 16)
    assert _local(occurrence) == datetime(2026, 3, 1, 9)
    assert _local_end(occurrence) == datetime(2026, 3, 1, 10)
    assert occurrence.is_all_day is False
    assert occurrence.is_override is False


def test_non_recurring_event_outside_window_is_dropped() -> None:
    assert expand(_master(), _utc(2026, 3, 1, 16), _utc(2026, 3, 1, 17)) == []


def test_event_starting_before_window_but_overlapping_is_included() -> None:
    occurrences = expand(_master(), _utc(2026, 3, 1, 15, 30), _utc(2026, 3, 1, 18))

    assert [_local(item) for item in occurrences] == [datetime(2026, 3, 1, 9)]


def test_event_ending_exactly_at_window_start_is_excluded() -> None:
    assert expand(_master(), _utc(2026, 3, 1, 16), _utc(2026, 3, 1, 18)) == []


def test_window_accepts_epoch_seconds_and_naive_datetimes() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1")

    epochs = expand(master, 1772371800, 1772379000)  # 2026-03-01 14:30..16:30 UTC
    naive = expand(master, datetime(2026, 3, 1, 14, 30), datetime(2026, 3, 1, 16, 30))

    assert [item.recurrence_key for item in epochs] == [item.recurrence_key for item in naive]


def test_inverted_or_empty_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="end_utc must be after start_utc"):
        expand(_master(), _utc(2026, 3, 1), _utc(2026, 3, 1))


def test_aware_non_utc_window_is_normalized() -> None:
    from zoneinfo import ZoneInfo

    start = datetime(2026, 3, 1, 9, 30, tzinfo=ZoneInfo(CHICAGO))
    occurrences = expand(_master(), start, start + timedelta(hours=1))

    assert len(occurrences) == 1


def test_weekly_spring_forward_keeps_wall_time() -> None:
    master = _master(rrule="FREQ=WEEKLY;BYDAY=SU;COUNT=4")

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_local(item) for item in occurrences] == [
        datetime(2026, 3, 1, 9),
        datetime(2026, 3, 8, 9),
        datetime(2026, 3, 15, 9),
        datetime(2026, 3, 22, 9),
    ]
    assert [item.start_utc for item in occurrences] == [
        _utc(2026, 3, 1, 15),
        _utc(2026, 3, 8, 14),
        _utc(2026, 3, 15, 14),
        _utc(2026, 3, 22, 14),
    ]
    assert [_local_end(item).hour for item in occurrences] == [10, 10, 10, 10]


def test_weekly_fall_back_keeps_wall_time() -> None:
    master = _master(
        start=datetime(2026, 10, 25, 9, 0),
        rrule="FREQ=WEEKLY;COUNT=3",
    )

    occurrences = expand(master, _utc(2026, 10, 20), _utc(2026, 11, 10))

    assert [_local(item) for item in occurrences] == [
        datetime(2026, 10, 25, 9),
        datetime(2026, 11, 1, 9),
        datetime(2026, 11, 8, 9),
    ]
    assert [item.start_utc for item in occurrences] == [
        _utc(2026, 10, 25, 14),
        _utc(2026, 11, 1, 15),
        _utc(2026, 11, 8, 15),
    ]


def test_nonexistent_local_time_uses_the_pre_gap_offset() -> None:
    master = _master(
        start=datetime(2026, 3, 8, 2, 30),
        rrule="FREQ=DAILY;COUNT=1",
    )

    occurrence = expand(master, _utc(2026, 3, 8), _utc(2026, 3, 9))[0]

    assert occurrence.start_utc == _utc(2026, 3, 8, 8, 30)
    assert _local(occurrence) == datetime(2026, 3, 8, 3, 30)


def test_ambiguous_local_time_uses_the_first_occurrence() -> None:
    master = _master(
        start=datetime(2026, 11, 1, 1, 30),
        rrule="FREQ=DAILY;COUNT=1",
    )

    occurrence = expand(master, _utc(2026, 11, 1), _utc(2026, 11, 2))[0]

    assert occurrence.start_utc == _utc(2026, 11, 1, 6, 30)
    assert _local(occurrence) == datetime(2026, 11, 1, 1, 30)


def test_monthly_rule_skips_months_without_the_requested_day() -> None:
    master = _master(
        start=datetime(2026, 1, 31, 9, 0),
        rrule="FREQ=MONTHLY;BYMONTHDAY=31;COUNT=4",
    )

    occurrences = expand(master, _utc(2026, 1, 1), _utc(2026, 9, 1))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 1, 31),
        date(2026, 3, 31),
        date(2026, 5, 31),
        date(2026, 7, 31),
    ]


def test_ordinal_byday_and_bysetpos_are_supported() -> None:
    last_friday = _master(
        start=datetime(2026, 3, 1, 9, 0),
        rrule="FREQ=MONTHLY;BYDAY=-1FR;COUNT=3",
    )
    last_weekday = _master(
        start=datetime(2026, 1, 1, 9, 0),
        rrule="FREQ=MONTHLY;BYSETPOS=-1;BYDAY=MO,TU,WE,TH,FR;COUNT=3",
    )

    assert [
        _local_date(item) for item in expand(last_friday, _utc(2026, 1, 1), _utc(2026, 7, 1))
    ] == [
        date(2026, 3, 27),
        date(2026, 4, 24),
        date(2026, 5, 29),
    ]
    assert [
        _local_date(item) for item in expand(last_weekday, _utc(2026, 1, 1), _utc(2026, 5, 1))
    ] == [
        date(2026, 1, 30),
        date(2026, 2, 27),
        date(2026, 3, 31),
    ]


def test_wkst_is_supported() -> None:
    master = _master(rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU;COUNT=3")

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 16),
        date(2026, 3, 30),
    ]


def test_leap_day_yearly_rule() -> None:
    master = _master(
        start=datetime(2024, 2, 29, 9, 0),
        rrule="FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=3",
    )

    occurrences = expand(master, _utc(2024, 1, 1), _utc(2033, 1, 1))

    assert [_local_date(item) for item in occurrences] == [
        date(2024, 2, 29),
        date(2028, 2, 29),
        date(2032, 2, 29),
    ]


def test_until_utc_is_inclusive_and_compares_instants() -> None:
    master = _master(
        rrule="FREQ=WEEKLY;BYDAY=SU;UNTIL=20260308T140000Z",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [item.start_utc for item in occurrences] == [
        _utc(2026, 3, 1, 15),
        _utc(2026, 3, 8, 14),
    ]


def test_until_local_wall_time_is_inclusive() -> None:
    master = _master(rrule="FREQ=DAILY;UNTIL=20260303T090000")

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 1),
        date(2026, 3, 2),
        date(2026, 3, 3),
    ]


def test_until_date_is_inclusive() -> None:
    master = _master(rrule="FREQ=DAILY;UNTIL=20260303")

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 1),
        date(2026, 3, 2),
        date(2026, 3, 3),
    ]


def test_until_stops_iteration_before_the_window_ends() -> None:
    master = _master(rrule="FREQ=DAILY;UNTIL=20260302T090000")

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert len(occurrences) == 2


def test_utc_until_comparison_breaks_on_the_first_candidate_past_it() -> None:
    master = _master(tz=None, rrule="FREQ=DAILY;UNTIL=20260302T090000Z")

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 1),
        date(2026, 3, 2),
    ]


def test_count_limits_the_rule_before_exdate_removal() -> None:
    keys = [
        item.recurrence_key
        for item in expand(_master(rrule="FREQ=DAILY;COUNT=3"), _utc(2026, 3, 1), _utc(2026, 3, 10))
    ]
    assert len(keys) == 3

    master = _master(rrule="FREQ=DAILY;COUNT=3", exdate=("20260302T090000",))
    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 1),
        date(2026, 3, 3),
    ]


def test_exdate_can_remove_dtstart_and_match_utc_values() -> None:
    master = _master(
        rrule="FREQ=DAILY;COUNT=3",
        exdate=("20260301T150000Z", "20260303T090000"),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [date(2026, 3, 2)]


def test_rdate_union_dedupe_and_count_independence() -> None:
    master = _master(
        rrule="FREQ=DAILY;COUNT=2",
        rdate=(
            "20260301T090000",  # duplicate of DTSTART
            "20260310T090000",  # beyond COUNT
            "20260310T140000Z",  # same instant as the previous local value
        ),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 1),
        date(2026, 3, 2),
        date(2026, 3, 10),
    ]


def test_rdate_period_is_reduced_to_its_start() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1", rdate=("20260305T090000/PT2H",))

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 3, 1),
        date(2026, 3, 5),
    ]


def test_rdate_can_be_removed_by_exdate() -> None:
    master = _master(
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("20260305T090000",),
        exdate=("20260305T090000",),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [date(2026, 3, 1)]


def test_rdate_before_dtstart_is_included() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1", rdate=("20260228T090000",))

    occurrences = expand(master, _utc(2026, 2, 20), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [
        date(2026, 2, 28),
        date(2026, 3, 1),
    ]


def test_rule_less_master_can_have_rdate_and_exdate() -> None:
    master = _master(rdate=("20260305T090000",), exdate=("20260301T090000",))

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [date(2026, 3, 5)]


def test_rdate_outside_the_window_is_not_returned() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1", rdate=("20260401T090000",))

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [_local_date(item) for item in occurrences] == [date(2026, 3, 1)]


def test_master_start_utc_is_trusted_for_dtstart_only() -> None:
    master = _master(
        start_utc=_utc(2026, 3, 1, 10),
        rrule="FREQ=DAILY;COUNT=2",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [item.start_utc for item in occurrences] == [
        _utc(2026, 3, 1, 10),
        _utc(2026, 3, 2, 15),
    ]


def test_floating_event_uses_utc_for_resolution() -> None:
    master = _master(tz=None, rrule="FREQ=DAILY;COUNT=1")

    occurrence = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2))[0]

    assert occurrence.start_utc == _utc(2026, 3, 1, 9)
    assert _local(occurrence) == datetime(2026, 3, 1, 9)


def test_rrule_prefix_is_accepted_by_expand() -> None:
    master = _master(rrule="RRULE:FREQ=DAILY;COUNT=2")

    assert len(expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))) == 2


def test_unsupported_rule_raises_from_expand() -> None:
    with pytest.raises(UnsupportedRecurrence, match="BYHOUR"):
        expand(_master(rrule="FREQ=DAILY;BYHOUR=9"), _utc(2026, 3, 1), _utc(2026, 3, 10))


def test_unknown_timezone_raises_instead_of_falling_back_to_utc() -> None:
    from zoneinfo import ZoneInfoNotFoundError

    master = _master(tz="Not/AZone")

    with pytest.raises(ZoneInfoNotFoundError):
        expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2))


def test_override_moves_an_instance_and_preserves_the_original_key() -> None:
    master = _master(rrule="FREQ=WEEKLY;COUNT=3")
    original = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))
    moved_key = original[1].recurrence_key
    override = OverrideEvent(
        recurrence_key=moved_key,
        start_local=datetime(2026, 3, 9, 14, 0),
        duration_seconds=1800,
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1), [override])

    assert [item.recurrence_key for item in occurrences] == [
        original[0].recurrence_key,
        moved_key,
        original[2].recurrence_key,
    ]
    moved = occurrences[1]
    assert _local(moved) == datetime(2026, 3, 9, 14)
    assert _local_end(moved) == datetime(2026, 3, 9, 14, 30)
    assert moved.is_override is True


def test_cancelled_override_removes_an_instance() -> None:
    master = _master(rrule="FREQ=WEEKLY;COUNT=3")
    original = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))
    override = OverrideEvent(
        recurrence_key=original[1].recurrence_key,
        start_local=datetime(2026, 3, 9, 9),
        duration_seconds=3600,
        status="CANCELLED",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1), [override])

    assert [item.recurrence_key for item in occurrences] == [
        original[0].recurrence_key,
        original[2].recurrence_key,
    ]


def test_override_with_unmatched_key_is_ignored() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1")
    override = OverrideEvent(
        recurrence_key="T:1",
        start_local=datetime(2026, 3, 2, 9),
        duration_seconds=3600,
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10), [override])

    assert len(occurrences) == 1


def test_override_moved_outside_the_window_is_dropped() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1")
    override = OverrideEvent(
        recurrence_key=expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2))[0].recurrence_key,
        start_local=datetime(2026, 4, 2, 9),
        duration_seconds=3600,
    )

    assert expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2), [override]) == []


def test_override_uses_its_own_zone_and_instant() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1")
    key = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2))[0].recurrence_key
    override = OverrideEvent(
        recurrence_key=key,
        start_local=datetime(2026, 3, 1, 8, 0),
        start_tz="America/New_York",
        start_utc=_utc(2026, 3, 1, 12, 0),
        duration_seconds=600,
    )

    occurrence = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2), [override])[0]

    assert occurrence.start_utc == _utc(2026, 3, 1, 12)
    assert _local(occurrence) == datetime(2026, 3, 1, 7)
    assert occurrence.is_override is True


def test_duplicate_overrides_are_rejected() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1")
    key = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2))[0].recurrence_key
    overrides = [
        OverrideEvent(recurrence_key=key, start_local=datetime(2026, 3, 2, 9), duration_seconds=1),
        OverrideEvent(recurrence_key=key, start_local=datetime(2026, 3, 3, 9), duration_seconds=1),
    ]

    with pytest.raises(ValueError, match="duplicate override"):
        expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2), overrides)


def test_empty_overrides_are_a_no_op() -> None:
    master = _master(rrule="FREQ=DAILY;COUNT=1")

    assert len(expand(master, _utc(2026, 3, 1), _utc(2026, 3, 2), [])) == 1


def test_all_day_event_has_exclusive_end_and_stable_utc_midnights() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 4),
        rrule="FREQ=WEEKLY;COUNT=3",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [(_date_start(item), _date_end(item)) for item in occurrences] == [
        (date(2026, 3, 2), date(2026, 3, 4)),
        (date(2026, 3, 9), date(2026, 3, 11)),
        (date(2026, 3, 16), date(2026, 3, 18)),
    ]
    assert occurrences[0].start_utc == _utc(2026, 3, 2)
    assert occurrences[0].end_utc == _utc(2026, 3, 4)
    assert occurrences[0].start_local is None
    assert occurrences[0].end_local is None
    assert occurrences[0].is_all_day is True


def test_all_day_date_window_is_half_open() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 4),
    )

    assert len(expand(master, _utc(2026, 3, 3), _utc(2026, 3, 4))) == 1
    assert expand(master, _utc(2026, 3, 4), _utc(2026, 3, 5)) == []


def test_all_day_rdate_and_exdate() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=WEEKLY;COUNT=2",
        rdate=("20260310", "20260302"),
        exdate=("20260309",),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 10),
    ]


def test_all_day_rdate_period_is_reduced_to_start_date() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("20260310/20260311",),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 10),
    ]


def test_all_day_rdate_outside_the_window_is_not_returned() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("20260501",),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [date(2026, 3, 2)]


def test_all_day_until_date_is_inclusive() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;UNTIL=20260304",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 3),
        date(2026, 3, 4),
    ]


def test_all_day_until_local_datetime() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;UNTIL=20260304T000000",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 3),
        date(2026, 3, 4),
    ]


def test_all_day_until_utc_datetime() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;UNTIL=20260304T000000Z",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 3),
        date(2026, 3, 4),
    ]


def test_all_day_until_none_and_break_on_window() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=10",
    )

    occurrences = expand(master, _utc(2026, 3, 2), _utc(2026, 3, 3))

    assert [_date_start(item) for item in occurrences] == [date(2026, 3, 2)]


def test_all_day_override_can_change_duration() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=2",
    )
    generated = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))
    override = OverrideEvent(
        recurrence_key=generated[0].recurrence_key,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 5),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1), [override])

    assert _date_start(occurrences[0]) == date(2026, 3, 2)
    assert _date_end(occurrences[0]) == date(2026, 3, 5)
    assert occurrences[0].is_override is True


def test_all_day_override_can_be_cancelled() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=2",
    )
    generated = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))
    override = OverrideEvent(
        recurrence_key=generated[0].recurrence_key,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        status="CANCELLED",
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1), [override])

    assert [_date_start(item) for item in occurrences] == [date(2026, 3, 3)]


def test_all_day_override_can_be_timed() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=1",
    )
    key = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))[0].recurrence_key
    override = OverrideEvent(
        recurrence_key=key,
        start_local=datetime(2026, 3, 2, 9),
        duration_seconds=3600,
    )

    occurrence = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1), [override])[0]

    assert occurrence.is_all_day is False
    assert occurrence.start_utc == _utc(2026, 3, 2, 9)


def test_all_day_rule_less_master_with_rdate() -> None:
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rdate=("20260305",),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 4, 1))

    assert [_date_start(item) for item in occurrences] == [
        date(2026, 3, 2),
        date(2026, 3, 5),
    ]


def test_timed_and_all_day_values_are_strict_about_value_type() -> None:
    with pytest.raises(ValueError, match="require DATE-TIME"):
        expand(
            _master(rrule="FREQ=DAILY;COUNT=1", rdate=("20260305",)),
            _utc(2026, 3, 1),
            _utc(2026, 3, 10),
        )

    all_day = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("20260305T090000",),
    )
    with pytest.raises(ValueError, match="require DATE"):
        expand(all_day, _utc(2026, 3, 1), _utc(2026, 3, 10))


def test_empty_recurrence_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="empty recurrence value"):
        expand(
            _master(rrule="FREQ=DAILY;COUNT=1", rdate=("  ",)), _utc(2026, 3, 1), _utc(2026, 3, 10)
        )

    all_day = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("",),
    )
    with pytest.raises(ValueError, match="empty recurrence value"):
        expand(all_day, _utc(2026, 3, 1), _utc(2026, 3, 10))


def test_malformed_recurrence_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="does not match format"):
        expand(
            _master(rrule="FREQ=DAILY;COUNT=1", rdate=("20261301T090000",)),
            _utc(2026, 3, 1),
            _utc(2026, 3, 10),
        )

    all_day = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("2026-03-02",),
    )
    with pytest.raises(ValueError, match="does not match format"):
        expand(all_day, _utc(2026, 3, 1), _utc(2026, 3, 10))


def test_expansion_limit_for_timed_series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expand_module, "_MAX_CANDIDATES", 2)
    master = _master(rrule="FREQ=DAILY")

    with pytest.raises(ExpansionLimitExceeded, match="exceeded 2"):
        expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))


def test_expansion_limit_for_all_day_series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expand_module, "_MAX_CANDIDATES", 2)
    master = _master(
        start=None,
        tz=None,
        duration=None,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 3),
        rrule="FREQ=DAILY",
    )

    with pytest.raises(ExpansionLimitExceeded, match="exceeded 2"):
        expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))


def test_occurrences_are_sorted_by_utc_start() -> None:
    master = _master(
        rrule="FREQ=DAILY;COUNT=1",
        rdate=("20260305T090000", "20260302T090000"),
    )

    occurrences = expand(master, _utc(2026, 3, 1), _utc(2026, 3, 10))

    assert [item.start_utc for item in occurrences] == sorted(
        item.start_utc for item in occurrences
    )
