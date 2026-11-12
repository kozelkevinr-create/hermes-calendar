"""Shared primitives for the iCalendar reader and writer.

The helpers here are deliberately small and dependency-free: the Hermes
environment does not ship the ``icalendar`` package, so RFC 5545 content-line
handling, TEXT escaping, DURATION values, and CRLF folding live in this module.

Failure modes are expressed as return values where the parser can recover
(``None``) and as :class:`IcsLimitError` where input must be rejected outright.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

#: Hard cap on the UTF-8 size of an accepted document (5 MiB).
MAX_ICS_BYTES = 5 * 1024 * 1024

#: Hard cap on the number of ``VEVENT`` components returned by one parse.
MAX_EVENTS = 10_000

#: ``PRODID`` emitted by :func:`hermes_calendar.ics.serialize_events`.
PRODID = "-//Hermes Calendar//EN"

_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)


class IcsLimitError(ValueError):
    """Raised when input exceeds a hard parser limit.

    The parser rejects oversized documents instead of truncating them: partial
    calendars hide data loss from the caller.
    """


def split_content_line(line: str) -> tuple[str, str]:
    """Split a content line at the first colon outside a quoted parameter.

    Returns ``(left, value)``; ``left`` still contains the property name and
    parameters. A line without a colon returns ``("", line)``.
    """
    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            return line[:index], line[index + 1 :]
    return "", line


def split_outside_quotes(text: str, separator: str) -> list[str]:
    """Split ``text`` on ``separator`` ignoring separators inside quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == separator and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def unquote(value: str) -> str:
    """Remove one pair of RFC 5545 double quotes from a parameter value."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def unescape_text(value: str) -> str:
    """Decode RFC 5545 TEXT escapes (``\\n``, ``\\,``, ``\\;``, ``\\\\``)."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            escaped = value[index + 1]
            if escaped in "nN":
                out.append("\n")
            else:
                # Unknown escapes are kept as the escaped character; keeping the
                # backslash would double-escape on the next round trip.
                out.append(escaped)
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def escape_text(value: str) -> str:
    """Encode a string as an RFC 5545 TEXT value."""
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace(";", "\\;").replace(",", "\\,")
    return escaped.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def split_escaped(value: str, separator: str = ",") -> list[str]:
    """Split an escaped TEXT value without splitting on escaped separators."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            current.append(char)
            current.append(value[index + 1])
            index += 2
        elif char == separator:
            parts.append("".join(current))
            current = []
            index += 1
        else:
            current.append(char)
            index += 1
    parts.append("".join(current))
    return parts


def parse_duration(value: str) -> int | None:
    """Parse an RFC 5545 DURATION into seconds, or ``None`` when malformed."""
    match = _DURATION_RE.match(value.strip())
    if match is None:
        return None
    groups = match.groupdict()
    if not any(groups[name] for name in ("weeks", "days", "hours", "minutes", "seconds")):
        return None
    try:
        total = (
            int(groups["weeks"] or 0) * 7 * 86_400
            + int(groups["days"] or 0) * 86_400
            + int(groups["hours"] or 0) * 3_600
            + int(groups["minutes"] or 0) * 60
            + int(groups["seconds"] or 0)
        )
    except ValueError:
        # Python bounds int() digit counts; a pathological DURATION is invalid.
        return None
    return -total if groups["sign"] == "-" else total


def format_duration(seconds: int) -> str:
    """Format signed seconds as an RFC 5545 DURATION value."""
    sign = "-" if seconds < 0 else ""
    remaining = abs(seconds)
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, secs = divmod(remaining, 60)
    out = f"{sign}P"
    if days:
        out += f"{days}D"
    if hours or minutes or secs or not days:
        out += "T"
        if hours:
            out += f"{hours}H"
        if minutes:
            out += f"{minutes}M"
        if secs or (not hours and not minutes):
            out += f"{secs}S"
    return out


def format_utc(epoch_seconds: int) -> str:
    """Format epoch seconds as a compact RFC 5545 UTC DATE-TIME value."""
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y%m%dT%H%M%SZ")


def format_local(value: datetime) -> str:
    """Format a naive wall clock as a compact DATE-TIME value."""
    return value.strftime("%Y%m%dT%H%M%S")


def clean_line_value(value: str) -> str:
    """Remove line breaks from a non-TEXT value before writing it."""
    return value.replace("\r", "").replace("\n", "")


def fold_line(line: str) -> str:
    """Fold one logical content line to at most 75 UTF-8 octets per line.

    Continuation lines start with a single space, which counts toward the
    octet limit. Splits never cut a multi-byte character.
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks: list[str] = []
    limit = 75
    while len(data) > limit:
        cut = limit
        while cut > 0 and data[cut] & 0xC0 == 0x80:
            cut -= 1
        chunks.append(data[:cut].decode("utf-8"))
        data = data[cut:]
        limit = 74
    chunks.append(data.decode("utf-8"))
    return "\r\n ".join(chunks)
