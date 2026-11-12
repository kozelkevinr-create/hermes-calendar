"""Pure, database-free recurrence expansion.

The public entry point is :func:`expand`. It implements the recurrence set in
``docs/design/core.md`` §6.1:

    {RRULE occurrences} ∪ {RDATE values} − {EXDATE values}
      with matching overrides replacing an occurrence (CANCELLED removes it)

Expansion is performed on naive local wall-clock datetimes and only then
resolved to UTC through ``zoneinfo``. That is what keeps a 09:00 meeting at
09:00 across a DST transition; see §6.4. The DST policy is RFC 5545 §3.3.5:
``fold=0`` means an ambiguous time uses the first (pre-transition) offset and
a nonexistent time is interpreted with the offset before the gap.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import cast
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]  # no py.typed marker

from hermes_calendar.recurrence.rule import ParsedRule, parse_rule
from hermes_calendar.recurrence.types import (
    MasterEvent,
    Occurrence,
    OverrideEvent,
    all_day_recurrence_key,
    timed_recurrence_key,
)

__all__ = ["ExpansionLimitExceeded", "expand"]

UtcInput = datetime | int | float

# The design asks for a hard cap on complex-rule iteration (§6.2). Expansion
# starts at DTSTART rather than fast-forwarding so COUNT remains exact; the cap
# bounds accidental huge windows.
_MAX_CANDIDATES = 100_000

_MIDNIGHT = time.min
_ONE_DAY = timedelta(days=1)


class ExpansionLimitExceeded(RuntimeError):
    """Raised when a window would require more than the safe candidate count."""


def expand(
    master: MasterEvent,
    start_utc: UtcInput,
    end_utc: UtcInput,
    overrides: Iterable[OverrideEvent] = (),
) -> list[Occurrence]:
    """Expand ``master`` into occurrences overlapping ``[start_utc, end_utc)``.

    Args:
        master: Recurrence-relevant fields of the master event row.
        start_utc: Window start as an aware/naive datetime or epoch seconds.
            Naive values are interpreted as UTC.
        end_utc: Exclusive window end, in the same forms as ``start_utc``.
        overrides: Per-instance replacements keyed by the original occurrence
            start (``OverrideEvent.recurrence_key``).

    Returns:
        Occurrences sorted by UTC start (and key for deterministic ties). For
        all-day events ``start_utc``/``end_utc`` are UTC-midnight sort keys and
        the authoritative values are ``start_date``/exclusive ``end_date``.

    Raises:
        UnsupportedRecurrence: the RRULE contains a part outside the P0 subset.
        ValueError: a value is malformed or a timing record is incoherent.
        ExpansionLimitExceeded: expansion exceeded the internal candidate cap.
    """
    window_start = _to_utc(start_utc)
    window_end = _to_utc(end_utc)
    if window_end <= window_start:
        raise ValueError("end_utc must be after start_utc")

    override_list = list(overrides)
    if master.is_all_day:
        occurrences = _expand_all_day(master, window_start, window_end, override_list)
    else:
        occurrences = _expand_timed(master, window_start, window_end, override_list)

    occurrences.sort(key=lambda item: (item.start_utc, item.recurrence_key))
    return occurrences


def _to_utc(value: UtcInput) -> datetime:
    """Normalize a datetime or epoch-seconds value to an aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime.fromtimestamp(value, tz=UTC)


def _zone_for(name: str | None) -> tzinfo:
    """Resolve an IANA zone; ``None`` means floating, resolved as UTC.

    A missing zone is never silently replaced with UTC: an unknown name raises
    ``ZoneInfoNotFoundError``. Floating events (no TZID) have no calendar zone
    in this pure interface, so UTC is used as their display/sort zone.
    """
    if name is None:
        return UTC
    return ZoneInfo(name)


def _resolve_local(local_dt: datetime, zone: tzinfo) -> datetime:
    """Resolve a naive wall time to a UTC instant using RFC 5545 DST rules."""
    return local_dt.replace(tzinfo=zone, fold=0).astimezone(UTC)


def _parse_timed_value(value: str, zone: tzinfo) -> tuple[str, datetime]:
    """Parse an RDATE/EXDATE value for a timed event.

    Returns ``(recurrence_key, utc_instant)``. A PERIOD is reduced to its
    start. A DATE value is invalid for a timed event and raises ``ValueError``
    rather than silently inventing a time.
    """
    token = value.split("/", 1)[0].strip()
    if not token:
        raise ValueError(f"empty recurrence value: {value!r}")

    if "T" in token.upper():
        if token.upper().endswith("Z"):
            instant = datetime.strptime(token.upper(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        else:
            local = datetime.strptime(token.upper(), "%Y%m%dT%H%M%S")
            instant = _resolve_local(local, zone)
        return timed_recurrence_key(instant), instant

    raise ValueError(f"timed events require DATE-TIME recurrence values, got {value!r}")


def _parse_all_day_value(value: str) -> tuple[str, date]:
    """Parse an RDATE/EXDATE value for an all-day event.

    A PERIOD is reduced to its start date. A DATE-TIME value is invalid for a
    date-based event and raises ``ValueError``.
    """
    token = value.split("/", 1)[0].strip()
    if not token:
        raise ValueError(f"empty recurrence value: {value!r}")

    token = token.upper()
    if "T" in token:
        raise ValueError(f"all-day events require DATE recurrence values, got {value!r}")
    day = datetime.strptime(token, "%Y%m%d").date()
    return all_day_recurrence_key(day), day


def _until_allows(
    instant: datetime,
    local_dt: datetime,
    until: date | datetime | None,
) -> bool:
    """Apply the inclusive UNTIL comparison policy from §6.1."""
    if until is None:
        return True
    if isinstance(until, datetime):
        if until.tzinfo is not None:
            return instant <= until.astimezone(UTC)
        return local_dt <= until
    return local_dt.date() <= until


def _until_local_bound(until: date | datetime | None, zone: tzinfo) -> datetime | None:
    """Return the largest local datetime that can satisfy ``until``.

    A UTC UNTIL is converted through the zone; the extra day keeps the iterate
    bound conservative across an offset change, and ``_until_allows`` performs
    the exact inclusive comparison.
    """
    if until is None:
        return None
    if isinstance(until, datetime):
        if until.tzinfo is None:
            return until
        converted = until.astimezone(zone).replace(tzinfo=None)
        return converted + _ONE_DAY
    return datetime.combine(until, time.max)


def _until_date_bound(until: date | datetime | None) -> date | None:
    """Return the exclusive last candidate date allowed by an all-day UNTIL."""
    if until is None:
        return None
    if isinstance(until, datetime):
        until_date = until.date() if until.tzinfo is None else until.astimezone(UTC).date()
        return until_date + _ONE_DAY
    return until + _ONE_DAY


def _iter_rule(rule: ParsedRule, dtstart: datetime) -> Iterator[datetime]:
    """Return a typed iterator for a dateutil rule."""
    return cast(Iterator[datetime], iter(rrulestr(rule.rrule, dtstart=dtstart)))


def _expand_timed(
    master: MasterEvent,
    window_start: datetime,
    window_end: datetime,
    overrides: list[OverrideEvent],
) -> list[Occurrence]:
    """Expand a timed master, then apply RDATE/EXDATE and overrides."""
    zone = _zone_for(master.start_tz)
    assert master.start_local is not None  # validated by MasterEvent
    assert master.duration_seconds is not None  # validated by MasterEvent
    dtstart = master.start_local
    duration = timedelta(seconds=master.duration_seconds)

    excluded = {_parse_timed_value(value, zone)[0] for value in master.exdate}
    generated: dict[str, Occurrence] = {}

    parsed_rule = parse_rule(master.rrule) if master.rrule is not None else None
    if parsed_rule is None:
        candidates: Iterator[datetime] = iter((dtstart,))
        local_break: datetime | None = None
    else:
        candidates = _iter_rule(parsed_rule, dtstart)
        local_break = _until_local_bound(parsed_rule.until, zone)

    window_break = window_end.astimezone(zone).replace(tzinfo=None) + duration
    if local_break is not None:
        window_break = min(window_break, local_break)

    for index, local_dt in enumerate(candidates):
        if index >= _MAX_CANDIDATES:
            raise ExpansionLimitExceeded(
                f"recurrence expansion exceeded {_MAX_CANDIDATES} candidates"
            )
        if local_dt > window_break:
            break
        instant = _resolve_local(local_dt, zone)
        if parsed_rule is not None and not _until_allows(instant, local_dt, parsed_rule.until):
            break

        if master.start_utc is not None and local_dt == dtstart:
            instant = _to_utc(master.start_utc)
        key = timed_recurrence_key(instant)
        if key in excluded or key in generated:
            continue
        occurrence = _timed_occurrence(key, instant, duration, zone)
        if _timed_overlaps(occurrence, window_start, window_end):
            generated[key] = occurrence

    for value in master.rdate:
        key, instant = _parse_timed_value(value, zone)
        if key in excluded or key in generated:
            continue
        occurrence = _timed_occurrence(key, instant, duration, zone)
        if _timed_overlaps(occurrence, window_start, window_end):
            generated[key] = occurrence

    result = _apply_overrides(list(generated.values()), overrides, zone)
    return [item for item in result if _overlaps(item, window_start, window_end)]


def _expand_all_day(
    master: MasterEvent,
    window_start: datetime,
    window_end: datetime,
    overrides: list[OverrideEvent],
) -> list[Occurrence]:
    """Expand an all-day master as date ranges with an exclusive end date."""
    assert master.start_date is not None  # validated by MasterEvent
    assert master.end_date is not None  # validated by MasterEvent
    duration_days = (master.end_date - master.start_date).days
    window_start_date = window_start.date()
    window_end_date = window_end.date()

    excluded = {_parse_all_day_value(value)[0] for value in master.exdate}
    generated: dict[str, Occurrence] = {}

    parsed_rule = parse_rule(master.rrule) if master.rrule is not None else None
    if parsed_rule is None:
        candidates: Iterator[datetime] = iter((datetime.combine(master.start_date, _MIDNIGHT),))
        date_break: date | None = None
    else:
        candidates = _iter_rule(parsed_rule, datetime.combine(master.start_date, _MIDNIGHT))
        date_break = _until_date_bound(parsed_rule.until)

    break_date = window_end_date
    if date_break is not None:
        break_date = min(break_date, date_break)

    for index, local_dt in enumerate(candidates):
        if index >= _MAX_CANDIDATES:
            raise ExpansionLimitExceeded(
                f"recurrence expansion exceeded {_MAX_CANDIDATES} candidates"
            )
        if local_dt.date() >= break_date:
            break

        key = all_day_recurrence_key(local_dt.date())
        if key in excluded or key in generated:
            continue
        occurrence = _all_day_occurrence(key, local_dt.date(), duration_days)
        if _all_day_overlaps(occurrence, window_start_date, window_end_date):
            generated[key] = occurrence

    for value in master.rdate:
        key, start_date = _parse_all_day_value(value)
        if key in excluded or key in generated:
            continue
        occurrence = _all_day_occurrence(key, start_date, duration_days)
        if _all_day_overlaps(occurrence, window_start_date, window_end_date):
            generated[key] = occurrence

    result = _apply_overrides(list(generated.values()), overrides, None)
    return [item for item in result if _overlaps(item, window_start, window_end)]


def _timed_occurrence(
    key: str,
    start_utc: datetime,
    duration: timedelta,
    zone: tzinfo,
    override: OverrideEvent | None = None,
) -> Occurrence:
    """Build a timed occurrence; duration is an exact instant delta (Q7)."""
    end_utc = start_utc + duration
    return Occurrence(
        recurrence_key=key,
        start_utc=start_utc,
        end_utc=end_utc,
        is_all_day=False,
        start_local=start_utc.astimezone(zone).replace(tzinfo=None),
        end_local=end_utc.astimezone(zone).replace(tzinfo=None),
        override=override,
    )


def _all_day_occurrence(
    key: str,
    start_date: date,
    duration_days: int,
    override: OverrideEvent | None = None,
) -> Occurrence:
    """Build an all-day occurrence; its end date is exclusive."""
    end_date = start_date + timedelta(days=duration_days)
    return Occurrence(
        recurrence_key=key,
        start_utc=_midnight_utc(start_date),
        end_utc=_midnight_utc(end_date),
        is_all_day=True,
        start_date=start_date,
        end_date=end_date,
        override=override,
    )


def _midnight_utc(value: date) -> datetime:
    """Return UTC midnight for a date, the design's stable all-day sort key."""
    return datetime.combine(value, _MIDNIGHT, tzinfo=UTC)


def _timed_overlaps(occurrence: Occurrence, start: datetime, end: datetime) -> bool:
    """Half-open instant overlap: ``start < window_end and end > window_start``."""
    return occurrence.start_utc < end and occurrence.end_utc > start


def _overlaps(occurrence: Occurrence, start: datetime, end: datetime) -> bool:
    """Dispatch overlap by the occurrence's final representation.

    An override may change an instance between timed and all-day, so the final
    window filter cannot assume the master's representation.
    """
    if occurrence.is_all_day:
        return _all_day_overlaps(occurrence, start.date(), end.date())
    return _timed_overlaps(occurrence, start, end)


def _all_day_overlaps(occurrence: Occurrence, start_date: date, end_date: date) -> bool:
    """Half-open date overlap, matching the all-day query in §6.8."""
    assert occurrence.start_date is not None  # always set for all-day
    assert occurrence.end_date is not None  # always set for all-day
    return occurrence.start_date < end_date and occurrence.end_date > start_date


def _apply_overrides(
    occurrences: list[Occurrence],
    overrides: list[OverrideEvent],
    master_zone: tzinfo | None,
) -> list[Occurrence]:
    """Replace generated occurrences with overrides; drop cancelled instances.

    Overrides only replace a generated occurrence for the same original key.
    The key is preserved on the replacement so callers can still map it back to
    the master series. A ``CANCELLED`` override removes the instance.
    """
    if not overrides:
        return occurrences

    by_key: dict[str, OverrideEvent] = {}
    for item in overrides:
        if item.recurrence_key in by_key:
            raise ValueError(f"duplicate override for {item.recurrence_key}")
        by_key[item.recurrence_key] = item

    result: list[Occurrence] = []
    for occurrence in occurrences:
        found = by_key.get(occurrence.recurrence_key)
        if found is None:
            result.append(occurrence)
        elif found.is_cancelled:
            continue
        else:
            result.append(_override_occurrence(occurrence.recurrence_key, found, master_zone))
    return result


def _override_occurrence(
    key: str,
    override: OverrideEvent,
    master_zone: tzinfo | None,
) -> Occurrence:
    """Build an occurrence from an override's own start/end (no inheritance)."""
    if override.is_all_day:
        assert override.start_date is not None  # validated by OverrideEvent
        assert override.end_date is not None  # validated by OverrideEvent
        return _all_day_occurrence(
            key,
            override.start_date,
            (override.end_date - override.start_date).days,
            override=override,
        )

    assert override.start_local is not None  # validated by OverrideEvent
    assert override.duration_seconds is not None  # validated by OverrideEvent
    zone = master_zone or UTC
    if override.start_tz is not None:
        zone = _zone_for(override.start_tz)
    instant = (
        _to_utc(override.start_utc)
        if override.start_utc is not None
        else _resolve_local(override.start_local, zone)
    )
    return _timed_occurrence(
        key,
        instant,
        timedelta(seconds=override.duration_seconds),
        zone,
        override=override,
    )
