"""Render a reminder into the minimal, sanitized chat payload.

Privacy rules from ``app-notifications.md`` §6.10 and §6.13:

* the title is the event summary with control characters removed, whitespace
  collapsed, mentions neutralized, and a 120-character cap;
* the body carries the title, a relative-time phrase, and either a configured
  dashboard link or "Open the calendar app" — never a description, location,
  attendee, URL from the event, or conferencing detail;
* a deep link is emitted only from the operator's configured dashboard origin;
  the plugin never invents a host or port.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ..db.reminders import ReminderInstance
from .models import Notification, NotificationSettings, Priority

MAX_TITLE = 120

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_MENTION = re.compile(r"(?i)@(everyone|channel|here|all)\b")
_SLACK_MENTION = re.compile(r"<(?=[@#!])")

# Hermes's `hermes send` path recognizes three message directives in the build
# this plugin targets: `MEDIA:<path>`, `[[audio_as_voice]]`, and
# `[[as_document]]`. The extraction is in
# ~/.hermes/hermes-agent/gateway/platforms/base.py:
#   * line 1312 `_has_media_directives` names all three;
#   * lines 3203-3248 `extract_media` strips the two bracket markers and runs
#     MEDIA_TAG_CLEANUP_RE / the extensionless validator;
#   * lines 1220-1227 define MEDIA_TAG_CLEANUP_RE (re.IGNORECASE, optional
#     quote/emphasis wrappers, whitespace after the colon);
#   * lines 1248-1253 define MEDIA_EXTENSIONLESS_TAG_RE for every other path.
# `tools/send_message_tool.py` lines 236-238 captures `[[as_document]]` and
# calls extract_media; `hermes_cli/send_cmd.py` lines 40-50 and 254 document
# MEDIA/[[as_document]]. Event text is untrusted (ICS import, other profiles),
# so we defuse every recognized spelling, including case, whitespace,
# zero-width characters, full-width colons, and full-width bracket forms. We
# replace the colon with U+2236 (a visible ratio-colon look-alike Hermes does
# not match) and break the bracket pair with spaces; nothing is deleted.
_GAP = r"[\s\u200b-\u200f\u2060\ufeff]*"
_NAME_GAP = r"[\s_\-\u200b-\u200f\u2060\ufeff]*"
_MEDIA_DIRECTIVE = re.compile(rf"(?i)M{_GAP}E{_GAP}D{_GAP}I{_GAP}A{_GAP}[:\uff1a\ufe55\ufe13]")
_BRACKET_OPEN = r"[\[\uff3b\u3010]"
_BRACKET_CLOSE = r"[\]\uff3d\u3011]"
_BRACKET_DIRECTIVE = re.compile(
    rf"(?i){_BRACKET_OPEN}{_GAP}{_BRACKET_OPEN}{_GAP}"
    rf"(as{_NAME_GAP}document|audio{_NAME_GAP}as{_NAME_GAP}voice)"
    rf"{_GAP}{_BRACKET_CLOSE}{_GAP}{_BRACKET_CLOSE}"
)
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def neutralize_mentions(text: str) -> str:
    """Defuse platform-wide mentions without dropping readable text.

    A zero-width space after ``@`` keeps the word visible but stops Discord,
    Slack, Telegram, and Matrix from treating it as a live mention. Slack's
    ``<@U…>``/``<!channel>`` forms lose their special opening bracket.
    """
    text = _MENTION.sub(lambda match: f"@\u200b{match.group(1)}", text)
    return _SLACK_MENTION.sub("<\u200b", text)


def _neutralize_bracket_directive(match: re.Match[str]) -> str:
    name = re.sub(r"[\s_-]+", "_", match.group(1).strip().lower())
    return f"[ [{name}] ]"


def neutralize_directives(text: str) -> str:
    """Defuse Hermes ``MEDIA:``, ``[[as_document]]`` and ``[[audio_as_voice]]``.

    The transformation is idempotent and keeps the title readable: a MEDIA
    colon becomes ``∶`` and a bracket directive becomes ``[ [name] ]``. Event
    summaries are untrusted, and ``hermes send`` treats a surviving directive
    as an instruction to attach a local file (``base.py`` lines 3203-3248).
    """
    text = _MEDIA_DIRECTIVE.sub(lambda match: f"{match.group(0)[:-1]}\u2236", text)
    return _BRACKET_DIRECTIVE.sub(_neutralize_bracket_directive, text)


def sanitize(text: str, *, cap: int = MAX_TITLE) -> str:
    """Strip control/zero-width characters, collapse whitespace, defuse injection."""
    cleaned = _ZERO_WIDTH.sub("", text)
    cleaned = _CONTROL.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = neutralize_mentions(cleaned)
    cleaned = neutralize_directives(cleaned)
    if len(cleaned) > cap:
        cleaned = cleaned[: cap - 1].rstrip() + "…"
    return cleaned


def _duration(seconds: int) -> str:
    seconds = abs(seconds)
    if seconds and seconds % 86_400 == 0:
        days = seconds // 86_400
        return f"{days} day" if days == 1 else f"{days} days"
    if seconds and seconds % 3_600 == 0:
        hours = seconds // 3_600
        return f"{hours} h"
    minutes = max(1, seconds // 60)
    return f"{minutes} min"


def relative_phrase(offset_seconds: int) -> str:
    """Describe the reminder offset relative to the occurrence start."""
    if offset_seconds == 0:
        return "starting now"
    if offset_seconds < 0:
        return f"in {_duration(offset_seconds)}"
    return f"{_duration(offset_seconds)} ago"


def render_notification(
    instance: ReminderInstance,
    *,
    summary: str,
    settings: NotificationSettings,
    late: bool = False,
) -> Notification:
    """Build the channel-neutral notification for one claimed instance."""
    base = sanitize(summary) or "Calendar reminder"
    phrase = relative_phrase(instance.offset_seconds)
    title = f"{base} {phrase}"
    if late:
        title = f"While you were away: {title}"
    title = sanitize(title)

    if settings.dashboard_url:
        target = f"{settings.dashboard_url}/calendar?event={quote(f'{instance.store_id}:{instance.event_id}', safe='')}"
        footer = f"Open the calendar to snooze or see details: {target}"
    else:
        footer = "Open the calendar app to snooze or see details."
    body = f"{title}\n{footer}"

    priority: Priority
    if instance.priority in {"digest", "normal", "high", "critical"}:
        priority = instance.priority  # type: ignore[assignment]
    else:  # pragma: no cover - the schema CHECK prevents this
        priority = "normal"

    return Notification(
        id=instance.dedup_key,
        title=title,
        body=body,
        priority=priority,
        store_id=instance.store_id,
        event_id=instance.event_id,
        occurrence_utc=instance.occurrence_start_utc,
        deep_link=(
            f"{settings.dashboard_url}/calendar?event={quote(f'{instance.store_id}:{instance.event_id}', safe='')}"
            if settings.dashboard_url
            else None
        ),
    )
