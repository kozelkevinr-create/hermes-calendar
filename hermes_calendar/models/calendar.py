"""Source and calendar models.

A source is where data comes from (local plugin data or an external sync peer);
a calendar is a named collection inside one source. Both carry the store scope
so exports and backups stay self-describing (``core.md`` §4.6).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class Source:
    """One source of calendar data."""

    id: str
    scope: str
    kind: str
    display_name: str
    owner_profile_id: str | None = None
    config: dict[str, object] | None = None
    capabilities: dict[str, object] | None = None
    created_utc: int = 0
    updated_utc: int = 0
    deleted_utc: int | None = None


@dataclass(frozen=True, kw_only=True)
class Calendar:
    """One calendar inside a source."""

    id: str
    source_id: str
    name: str
    scope: str = "profile"
    owner_profile_id: str | None = None
    remote_id: str | None = None
    description: str | None = None
    color: str | None = None
    default_tz: str = "UTC"
    read_only: bool = False
    sync_token: str | None = None
    ctag: str | None = None
    created_utc: int = 0
    updated_utc: int = 0
    deleted_utc: int | None = None
