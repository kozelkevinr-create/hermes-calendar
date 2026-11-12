"""Property-style comparison against dateutil for random simple rules.

These tests are deliberately in UTC so they validate the wrapper (key
generation, windowing, ordering, COUNT handling) rather than re-testing the
DST policy; the DST cases live in ``test_recurrence_expand.py``.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]  # no py.typed marker

from hermes_calendar.recurrence import MasterEvent, expand

_FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _random_rule(rng: random.Random, frequency: str) -> str:
    parts = [
        f"FREQ={frequency}",
        f"INTERVAL={rng.choice((1, 2, 3))}",
        f"COUNT={rng.randint(1, 12)}",
    ]

    if frequency in {"WEEKLY", "MONTHLY"}:
        days = rng.sample(_WEEKDAYS, rng.randint(1, 3))
        if frequency == "MONTHLY" and rng.random() < 0.3:
            days = [f"{rng.choice((-1, 1, 2))}{day}" for day in days[:1]]
        parts.append(f"BYDAY={','.join(days)}")
    elif frequency == "YEARLY":
        parts.append(f"BYMONTH={rng.randint(1, 12)}")
        parts.append(f"BYMONTHDAY={rng.choice((-1, 1, 15, 28))}")
    elif frequency == "DAILY" and rng.random() < 0.4:
        parts.append(f"BYDAY={rng.choice(_WEEKDAYS)}")

    return ";".join(parts)


def test_random_simple_rules_match_dateutil() -> None:
    rng = random.Random(20261112)
    dtstart = datetime(2026, 1, 1, 9, 0)

    for _ in range(60):
        frequency = rng.choice(_FREQUENCIES)
        rule = _random_rule(rng, frequency)
        master = MasterEvent(
            start_local=dtstart,
            duration_seconds=3600,
            rrule=rule,
        )

        expected = list(rrulestr(rule, dtstart=dtstart))
        actual = expand(master, datetime(2025, 12, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))

        assert [item.start_utc for item in actual] == [
            item.replace(tzinfo=UTC) for item in expected
        ], rule
        assert [item.start_local for item in actual] == expected, rule
