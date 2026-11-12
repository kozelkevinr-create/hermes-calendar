"""Tests for the supported RRULE parser and UNTIL policies."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from hermes_calendar.recurrence import UnsupportedRecurrence
from hermes_calendar.recurrence.rule import parse_rule, parse_until


def test_parse_rule_accepts_the_whole_p0_subset() -> None:
    parsed = parse_rule(
        "FREQ=WEEKLY;INTERVAL=2;COUNT=4;UNTIL=20260401T090000Z;"
        "BYDAY=MO,-1FR;BYMONTHDAY=1,-1;BYMONTH=3,4;BYSETPOS=-1;WKST=SU"
    )

    assert parsed.until == datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
    assert parsed.rrule == (
        "FREQ=WEEKLY;INTERVAL=2;COUNT=4;"
        "BYDAY=MO,-1FR;BYMONTHDAY=1,-1;BYMONTH=3,4;BYSETPOS=-1;WKST=SU"
    )


def test_parse_rule_accepts_prefix_and_lowercase() -> None:
    parsed = parse_rule("rrule:freq=daily;count=2")

    assert parsed.rrule == "FREQ=DAILY;COUNT=2"
    assert parsed.until is None


def test_parse_rule_without_until() -> None:
    parsed = parse_rule("FREQ=DAILY;COUNT=2")

    assert parsed.until is None


@pytest.mark.parametrize(
    "part",
    [
        "BYSECOND=0",
        "BYMINUTE=30",
        "BYHOUR=9",
        "BYYEARDAY=1",
        "BYWEEKNO=2",
        "RSCALE=CHINESE",
        "SKIP=OMIT",
    ],
)
def test_parse_rule_rejects_unsupported_parts(part: str) -> None:
    with pytest.raises(UnsupportedRecurrence, match=part.split("=")[0]):
        parse_rule(f"FREQ=DAILY;{part}")


def test_parse_rule_rejects_unknown_parts() -> None:
    with pytest.raises(UnsupportedRecurrence, match="X-FOO"):
        parse_rule("FREQ=DAILY;X-FOO=1")


@pytest.mark.parametrize("frequency", ["HOURLY", "MINUTELY", "SECONDLY"])
def test_parse_rule_rejects_unsupported_frequencies(frequency: str) -> None:
    with pytest.raises(UnsupportedRecurrence, match=f"FREQ={frequency}"):
        parse_rule(f"FREQ={frequency}")


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ("FREQ=DAILY;;COUNT=2", "malformed RRULE part"),
        ("FREQ", "malformed RRULE part"),
        ("=DAILY", "malformed RRULE part"),
        ("FREQ=", "malformed RRULE part"),
        ("FREQ=DAILY;FREQ=WEEKLY", "duplicate RRULE part"),
        ("FREQ=DAILY;UNTIL=20260401;UNTIL=20260402", "duplicate RRULE part"),
        ("COUNT=2", "missing FREQ"),
    ],
)
def test_parse_rule_rejects_malformed_rules(rule: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_rule(rule)


def test_parse_until_accepts_date_local_and_utc() -> None:
    assert parse_until("20260401") == date(2026, 4, 1)
    assert parse_until("20260401T090000") == datetime(2026, 4, 1, 9, 0)
    assert parse_until("20260401T090000Z") == datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-04-01", "20260401T0900", "20260401T090000X", ""])
def test_parse_until_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError, match="malformed UNTIL"):
        parse_until(value)
