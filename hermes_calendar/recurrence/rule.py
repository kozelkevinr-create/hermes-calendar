"""Parsing and validation of the supported RFC 5545 ``RRULE`` subset.

The recurrence engine delegates the calendar grammar to ``python-dateutil``
(a pure-Python dependency available in the Hermes runtime) and keeps only the
policy that the design makes ours:

* the P0 subset from ``docs/design/core.md`` §6.2;
* any other rule part is rejected with :class:`UnsupportedRecurrence` instead
  of being silently dropped;
* ``UNTIL`` is removed before dateutil sees the rule so the design's
  instant/wall-time/date comparison rules can be applied explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

P0_RULE_PARTS = frozenset(
    {
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "BYDAY",
        "BYMONTHDAY",
        "BYMONTH",
        "BYSETPOS",
        "WKST",
    }
)

SUPPORTED_FREQUENCIES = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})

_DATE_RE = re.compile(r"^\d{8}$")
_DATE_TIME_RE = re.compile(r"^\d{8}T\d{6}Z?$")


class UnsupportedRecurrence(ValueError):
    """Raised when an RRULE uses a part outside the P0 subset."""


@dataclass(frozen=True, slots=True)
class ParsedRule:
    """A rule string safe for dateutil plus the explicitly parsed ``UNTIL``.

    ``rrule`` has no ``UNTIL`` part; ``until`` is a :class:`datetime.date` for
    a DATE value, a naive datetime for a local DATE-TIME, or an aware UTC
    datetime for a ``Z`` value. ``None`` means the rule has no bound.
    """

    rrule: str
    until: date | datetime | None


def parse_rule(rule: str) -> ParsedRule:
    """Validate ``rule`` and split out ``UNTIL``.

    Raises:
        UnsupportedRecurrence: a rule part, or a ``FREQ`` value, is outside
            the P0 subset. The message names every offending part.
        ValueError: the rule is structurally malformed or has no ``FREQ``.
    """
    text = rule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :].strip()

    parts: list[str] = []
    seen: set[str] = set()
    until: date | datetime | None = None

    for raw_part in text.split(";"):
        name, separator, value = raw_part.partition("=")
        name = name.strip().upper()
        value = value.strip().upper()
        if not separator or not name or not value:
            raise ValueError(f"malformed RRULE part: {raw_part!r}")
        if name in seen:
            raise ValueError(f"duplicate RRULE part: {name}")
        seen.add(name)
        if name == "UNTIL":
            until = parse_until(value)
        else:
            parts.append(f"{name}={value}")

    unsupported = sorted(seen - P0_RULE_PARTS)
    if unsupported:
        raise UnsupportedRecurrence(
            f"unsupported RRULE part(s): {', '.join(unsupported)} in {rule!r}"
        )

    if "FREQ" not in seen:
        raise ValueError(f"RRULE is missing FREQ: {rule!r}")

    frequency = next(part.split("=", 1)[1] for part in parts if part.startswith("FREQ="))
    if frequency not in SUPPORTED_FREQUENCIES:
        raise UnsupportedRecurrence(f"unsupported RRULE FREQ={frequency} in {rule!r}")

    return ParsedRule(rrule=";".join(parts), until=until)


def parse_until(value: str) -> date | datetime:
    """Parse an RFC 5545 ``UNTIL`` value into a date or datetime.

    The returned object's type encodes the comparison policy: dates compare
    dates, naive datetimes compare wall times, and aware datetimes compare
    instants (``docs/design/core.md`` §6.1).
    """
    if _DATE_RE.fullmatch(value):
        return datetime.strptime(value, "%Y%m%d").date()
    if _DATE_TIME_RE.fullmatch(value):
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    raise ValueError(f"malformed UNTIL value: {value!r}")
