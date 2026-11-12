"""Value objects shared by recurrence parsing and expansion.

These types are the narrow, storage-independent interface of the recurrence
engine. The database layer maps its columns into :class:`MasterEvent` and
:class:`OverrideEvent`; nothing in this package imports Hermes or SQLite.

A ``MasterEvent`` (or ``OverrideEvent``) carries exactly one of two timing
representations, matching the ``events`` table checks in
``docs/design/core.md`` §5.3:

* timed: ``start_local`` + ``duration_seconds`` (+ optional ``start_tz``);
* all-day: ``start_date`` + exclusive ``end_date``.

Local wall times are naive datetimes. Instants passed to and returned from the
engine are timezone-aware UTC datetimes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

ALL_DAY = "D"
TIMED = "T"

CONFIRMED = "CONFIRMED"
TENTATIVE = "TENTATIVE"
CANCELLED = "CANCELLED"

_STATUSES = frozenset({CONFIRMED, TENTATIVE, CANCELLED})


def timed_recurrence_key(start_utc: datetime) -> str:
    """Return the canonical ``recurrence_key`` for a timed occurrence.

    The key is ``T:<epoch-seconds>`` (``docs/design/core.md`` §3.3). It is
    computed from the original occurrence instant, before an override moves
    the instance, so calendar time zones never change the key.
    """
    return f"{TIMED}:{int(start_utc.timestamp())}"


def all_day_recurrence_key(start_date: date) -> str:
    """Return the canonical ``recurrence_key`` for an all-day occurrence."""
    return f"{ALL_DAY}:{start_date.isoformat()}"


def _validate_timing(
    *,
    start_local: datetime | None,
    start_tz: str | None,
    duration_seconds: int | None,
    start_date: date | None,
    end_date: date | None,
) -> None:
    """Reject ambiguous or incoherent timing records."""
    if (start_date is None) != (end_date is None):
        raise ValueError("all-day timing requires both start_date and end_date")

    if start_date is not None:
        if start_local is not None:
            raise ValueError("an event cannot be both all-day and timed")
        if duration_seconds is not None:
            raise ValueError("all-day events use end_date, not duration_seconds")
        assert end_date is not None
        if end_date <= start_date:
            raise ValueError("all-day end_date must be after start_date")
        return

    if start_local is None:
        raise ValueError("timed events require start_local")
    if start_local.tzinfo is not None:
        raise ValueError("start_local must be a naive wall-clock datetime")
    if duration_seconds is None:
        raise ValueError("timed events require duration_seconds")
    if duration_seconds < 0:
        raise ValueError("duration_seconds must not be negative")
    if start_tz is not None and not start_tz.strip():
        raise ValueError("start_tz must be a non-empty IANA zone name when set")


@dataclass(frozen=True, slots=True)
class MasterEvent:
    """The recurrence-relevant fields of a master event row.

    ``rrule`` is the verbatim RFC 5545 value without the ``RRULE:`` prefix.
    ``rdate`` and ``exdate`` are RFC value strings (``YYYYMMDD``,
    ``YYYYMMDDTHHMMSS``, or ``YYYYMMDDTHHMMSSZ``); a ``PERIOD`` value is
    reduced to its start. ``start_utc`` is optional because the engine can
    resolve ``start_local`` in ``start_tz``; when supplied it is trusted for
    the ``DTSTART`` instant so imported instants are preserved exactly.
    """

    start_local: datetime | None = None
    start_tz: str | None = None
    start_utc: datetime | None = None
    duration_seconds: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    rrule: str | None = None
    rdate: Sequence[str] = ()
    exdate: Sequence[str] = ()

    def __post_init__(self) -> None:
        _validate_timing(
            start_local=self.start_local,
            start_tz=self.start_tz,
            duration_seconds=self.duration_seconds,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        if self.rrule is not None and not self.rrule.strip():
            raise ValueError("rrule must be non-empty when present")

    @property
    def is_all_day(self) -> bool:
        """Whether this record uses the date-range representation."""
        return self.start_date is not None


@dataclass(frozen=True, slots=True)
class OverrideEvent:
    """One per-instance replacement, keyed by the *original* occurrence start.

    ``recurrence_key`` is the key the master expansion produced for the
    replaced occurrence. It is preserved on the returned :class:`Occurrence`
    even when the override moves the instance.
    """

    recurrence_key: str
    start_local: datetime | None = None
    start_tz: str | None = None
    start_utc: datetime | None = None
    duration_seconds: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str = CONFIRMED

    def __post_init__(self) -> None:
        _validate_timing(
            start_local=self.start_local,
            start_tz=self.start_tz,
            duration_seconds=self.duration_seconds,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        if not self.recurrence_key.strip():
            raise ValueError("override recurrence_key must be non-empty")
        normalized = self.status.upper()
        if normalized not in _STATUSES:
            raise ValueError(f"unknown override status: {self.status!r}")
        object.__setattr__(self, "status", normalized)

    @property
    def is_all_day(self) -> bool:
        """Whether this override uses the date-range representation."""
        return self.start_date is not None

    @property
    def is_cancelled(self) -> bool:
        """Whether the override removes its occurrence from the series."""
        return self.status == CANCELLED


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One expanded instance of a series.

    ``recurrence_key`` identifies the *original* occurrence. For a moved
    override, ``start/end`` reflect the override while the key still points at
    the slot in the master series. Timed occurrences have ``start_local`` and
    ``end_local``; all-day occurrences have ``start_date`` and exclusive
    ``end_date``. ``start_utc``/``end_utc`` are always populated (all-day
    events use UTC midnight as a stable sort key, per the design).
    """

    recurrence_key: str
    start_utc: datetime
    end_utc: datetime
    is_all_day: bool
    start_local: datetime | None = None
    end_local: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    override: OverrideEvent | None = field(default=None)

    @property
    def is_override(self) -> bool:
        """Whether this occurrence was replaced by an override row."""
        return self.override is not None
