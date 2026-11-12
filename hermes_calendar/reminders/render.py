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


def neutralize_mentions(text: str) -> str:
    """Defuse platform-wide mentions without dropping readable text.

    A zero-width space after ``@`` keeps the word visible but stops Discord,
    Slack, Telegram, and Matrix from treating it as a live mention. Slack's
    ``<@U…>``/``<!channel>`` forms lose their special opening bracket.
    """
    text = _MENTION.sub(lambda match: f"@\u200b{match.group(1)}", text)
    return _SLACK_MENTION.sub("<\u200b", text)


def sanitize(text: str, *, cap: int = MAX_TITLE) -> str:
    """Strip control characters, collapse whitespace, defuse mentions, cap length."""
    cleaned = _CONTROL.sub(" ", text)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = neutralize_mentions(cleaned)
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
