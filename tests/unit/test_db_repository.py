"""Repository tests: events, calendars, change capture, and global contention."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hermes_calendar.db import (
    CalendarNotFoundError,
    EventNotFoundError,
    Repository,
    RevisionConflict,
    SourceNotFoundError,
    connect,
    migrate,
    open_global_store,
)
from hermes_calendar.db.migrations import GLOBAL_CALENDAR_ID, LOCAL_SOURCE_ID
from hermes_calendar.models import Attendee, EventDraft, Reminder

PROFILE = "coding"


def make_draft(calendar_id: str, **overrides: Any) -> EventDraft:
    values: dict[str, Any] = {
        "calendar_id": calendar_id,
        "start_utc": 1_000,
        "end_utc": 4_600,
        "start_local": "2026-03-02T09:00:00",
        "start_tz": "America/New_York",
        "duration_seconds": 3_600,
        "summary": "Weekly design review",
    }
    values.update(overrides)
    return EventDraft(**values)


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    connection = connect(tmp_path / "calendar.db")
    migrate(
        connection,
        store_scope="profile",
        store_id=f"profile:{PROFILE}",
        owner_profile_id=PROFILE,
    )
    try:
        yield Repository(connection)
    finally:
        connection.close()


@pytest.fixture
def global_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Repository]:
    home = tmp_path / "root" / "profiles" / PROFILE
    monkeypatch.setenv("HERMES_HOME", str(home))
    connection = open_global_store()
    try:
        yield Repository(connection)
    finally:
        connection.close()


def _logs(repository: Repository) -> list[sqlite3.Row]:
    return repository.connection.execute("SELECT * FROM change_log ORDER BY seq").fetchall()


def test_create_calendar_and_list_calendars(repository: Repository) -> None:
    work = repository.create_calendar(
        "Work",
        description="Day job",
        color="#336699",
        default_tz="Europe/Paris",
        read_only=True,
        remote_id="remote-work",
    )
    home = repository.create_calendar("Home")

    assert work.id.startswith("cal_")
    assert work.source_id == LOCAL_SOURCE_ID
    assert work.scope == "profile"
    assert work.owner_profile_id == PROFILE
    assert work.description == "Day job"
    assert work.color == "#336699"
    assert work.default_tz == "Europe/Paris"
    assert work.read_only is True
    assert work.remote_id == "remote-work"
    assert home.read_only is False
    assert [calendar.name for calendar in repository.list_calendars()] == ["Home", "Work"]
    assert [row["entity_type"] for row in _logs(repository)] == ["calendar", "calendar"]


def test_create_calendar_with_explicit_source_and_id(repository: Repository) -> None:
    repository.connection.execute(
        """
        INSERT INTO sources
          (id, scope, owner_profile_id, kind, display_name,
           config_json, capabilities_json, created_utc, updated_utc)
        VALUES ('src_caldav', 'profile', ?, 'caldav', 'CalDAV', '{}', '{}', 0, 0)
        """,
        (PROFILE,),
    )

    calendar = repository.create_calendar(
        "Remote",
        calendar_id="cal_remote",
        source_id="src_caldav",
    )

    assert calendar.id == "cal_remote"
    assert calendar.source_id == "src_caldav"
    assert calendar.owner_profile_id == PROFILE


def test_create_calendar_rejects_unknown_source(repository: Repository) -> None:
    with pytest.raises(SourceNotFoundError):
        repository.create_calendar("Broken", source_id="src_missing")


def test_create_calendar_rejects_deleted_source(repository: Repository) -> None:
    repository.connection.execute(
        "UPDATE sources SET deleted_utc = created_utc WHERE id = ?", (LOCAL_SOURCE_ID,)
    )

    with pytest.raises(SourceNotFoundError):
        repository.create_calendar("Broken", source_id=LOCAL_SOURCE_ID)


def test_create_calendar_requires_a_local_source(repository: Repository) -> None:
    repository.connection.execute("DELETE FROM sources")

    with pytest.raises(SourceNotFoundError, match="local"):
        repository.create_calendar("Broken")


def test_create_event_round_trip(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")

    event = repository.create_event(
        make_draft(
            calendar.id,
            summary="Weekly design review",
            description="Bring the schema draft.",
            location="Room 4B",
            url="https://meet.example.com/design",
            end_local="2026-03-02T10:00:00",
            end_tz="America/New_York",
            sequence=2,
            status="TENTATIVE",
            transparency="TRANSPARENT",
            classification="PRIVATE",
            organizer_email="dana@example.com",
            organizer_name="Dana",
            categories=("work", "design"),
            xprops={"X-TEST": "kept"},
            rrule_unsupported=("BYSECOND",),
            rdate=("2026-03-03T09:00:00Z",),
            exdate=("2026-03-04T09:00:00Z",),
            remote_href="/cal/work/1.ics",
            etag='"v1"',
            created_utc=789,
            dtstamp_utc=790,
            last_modified_utc=791,
        ),
        actor=PROFILE,
    )

    assert event.id
    assert event.uid.endswith("@hermes.local")
    assert event.calendar_id == calendar.id
    assert event.source_id == LOCAL_SOURCE_ID
    assert event.scope == "profile"
    assert event.owner_profile_id == PROFILE
    assert event.rev == 1
    assert event.summary == "Weekly design review"
    assert event.description == "Bring the schema draft."
    assert event.location == "Room 4B"
    assert event.url == "https://meet.example.com/design"
    assert event.start_utc == 1_000
    assert event.end_utc == 4_600
    assert event.duration_seconds == 3_600
    assert event.sequence == 2
    assert event.status == "TENTATIVE"
    assert event.transparency == "TRANSPARENT"
    assert event.classification == "PRIVATE"
    assert event.organizer_email == "dana@example.com"
    assert event.organizer_name == "Dana"
    assert event.categories == ("work", "design")
    assert event.xprops == {"X-TEST": "kept"}
    assert event.rrule_unsupported == ("BYSECOND",)
    assert event.rdate == ("2026-03-03T09:00:00Z",)
    assert event.exdate == ("2026-03-04T09:00:00Z",)
    assert event.remote_href == "/cal/work/1.ics"
    assert event.etag == '"v1"'
    assert event.created_utc == 789
    assert event.dtstamp_utc == 790
    assert event.last_modified_utc == 791
    assert event.deleted_utc is None
    assert repository.get_event(event.id) == event


def test_create_event_with_children_bumps_rev(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    attendees = (
        Attendee("mailto:alex@example.com", email="alex@example.com", name="Alex"),
        Attendee("mailto:sam@example.com", email="sam@example.com", rsvp=True, cutype="GROUP"),
    )
    reminder = Reminder(offset_seconds=-600, description="10 minutes")

    event = repository.create_event(
        make_draft(calendar.id, attendees=attendees, reminders=(reminder,)),
        actor=PROFILE,
    )

    assert event.rev == 4
    assert [attendee.address for attendee in event.attendees] == [
        "mailto:alex@example.com",
        "mailto:sam@example.com",
    ]
    assert event.attendees[1].rsvp is True
    assert event.attendees[1].cutype == "GROUP"
    assert len(event.reminders) == 1
    assert event.reminders[0].id is not None
    assert event.reminders[0].offset_seconds == -600
    assert event.reminders[0].description == "10 minutes"
    assert [
        (row["op"], row["rev"]) for row in _logs(repository) if row["entity_type"] == "event"
    ] == [
        ("insert", 1),
        ("update", 2),
        ("update", 3),
        ("update", 4),
    ]


def test_create_event_rejects_unknown_calendar(repository: Repository) -> None:
    with pytest.raises(CalendarNotFoundError):
        repository.create_event(make_draft("cal_missing"))


def test_create_event_rejects_deleted_calendar(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    repository.connection.execute(
        "UPDATE calendars SET deleted_utc = created_utc WHERE id = ?", (calendar.id,)
    )

    with pytest.raises(CalendarNotFoundError):
        repository.create_event(make_draft(calendar.id))


def test_create_event_computes_duration(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")

    event = repository.create_event(
        make_draft(calendar.id, duration_seconds=None, start_utc=10, end_utc=3_610)
    )

    assert event.duration_seconds == 3_600


def test_create_all_day_event(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")

    event = repository.create_event(
        make_draft(
            calendar.id,
            start_utc=0,
            end_utc=86_400,
            start_local=None,
            start_tz=None,
            duration_seconds=None,
            start_date="2026-03-02",
            end_date="2026-03-03",
            is_all_day=True,
        )
    )

    assert event.is_all_day is True
    assert event.start_date == "2026-03-02"
    assert event.end_date == "2026-03-03"
    assert event.duration_seconds is None


def test_create_floating_event(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")

    event = repository.create_event(make_draft(calendar.id, start_tz=None, is_floating=True))

    assert event.is_floating is True
    assert event.start_tz is None


def test_get_event_returns_none_for_unknown_id(repository: Repository) -> None:
    assert repository.get_event("missing") is None


def test_update_event_replaces_fields(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(make_draft(calendar.id), actor=PROFILE)

    updated = repository.update_event(
        replace(event, summary="Moved", location="Room 4B"),
        expected_rev=event.rev,
    )

    assert updated.rev == event.rev + 1
    assert updated.summary == "Moved"
    assert updated.location == "Room 4B"
    assert repository.get_event(event.id) == updated
    assert _logs(repository)[-1]["op"] == "update"


def test_update_event_does_not_churn_equivalent_children(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(
        make_draft(calendar.id, attendees=(Attendee("mailto:a@example.com"),)),
        actor=PROFILE,
    )

    updated = repository.update_event(
        replace(event, summary="Renamed"),
        expected_rev=event.rev,
    )

    assert updated.rev == event.rev + 1
    assert updated.attendees == event.attendees


def test_update_event_replaces_children(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(
        make_draft(
            calendar.id,
            attendees=(Attendee("mailto:old@example.com"),),
            reminders=(Reminder(offset_seconds=-300),),
        ),
        actor=PROFILE,
    )

    updated = repository.update_event(
        replace(
            event,
            attendees=(Attendee("mailto:new@example.com", email="new@example.com"),),
            reminders=(),
        ),
        expected_rev=event.rev,
    )

    assert updated.rev > event.rev
    assert [attendee.address for attendee in updated.attendees] == ["mailto:new@example.com"]
    assert updated.reminders == ()
    assert repository.get_event(event.id) == updated


def test_update_event_rejects_immutable_fields(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(make_draft(calendar.id), actor=PROFILE)

    with pytest.raises(ValueError, match="immutable"):
        repository.update_event(replace(event, uid="other"), expected_rev=event.rev)

    stored = repository.get_event(event.id)
    assert stored is not None
    assert stored.uid == event.uid


def test_update_event_missing(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(make_draft(calendar.id))

    with pytest.raises(EventNotFoundError):
        repository.update_event(replace(event, id="missing"), expected_rev=event.rev)


def test_update_event_stale_revision(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(make_draft(calendar.id))
    repository.update_event(replace(event, summary="First"), expected_rev=event.rev)

    with pytest.raises(RevisionConflict):
        repository.update_event(replace(event, summary="Second"), expected_rev=event.rev)

    stored = repository.get_event(event.id)
    assert stored is not None
    assert stored.summary == "First"


def test_delete_event_soft_deletes_idempotently(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(
        make_draft(calendar.id, etag='"v1"', remote_href="/e/1.ics"),
        actor=PROFILE,
    )

    repository.delete_event(event.id, expected_rev=event.rev, actor=PROFILE)

    stored = repository.get_event(event.id)
    assert stored is not None
    assert stored.deleted_utc is not None
    assert stored.rev == event.rev + 1
    assert stored.etag == '"v1"'
    assert stored.remote_href == "/e/1.ics"
    assert _logs(repository)[-1]["op"] == "delete"

    repository.delete_event(event.id)
    again = repository.get_event(event.id)
    assert again is not None
    assert again.rev == stored.rev


def test_delete_event_stale_revision(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    event = repository.create_event(make_draft(calendar.id))
    repository.update_event(replace(event, summary="Changed"), expected_rev=event.rev)

    with pytest.raises(RevisionConflict):
        repository.delete_event(event.id, expected_rev=event.rev)

    stored = repository.get_event(event.id)
    assert stored is not None
    assert stored.deleted_utc is None


def test_delete_event_missing(repository: Repository) -> None:
    with pytest.raises(EventNotFoundError):
        repository.delete_event("missing")


def test_list_events_filters_and_orders(repository: Repository) -> None:
    calendar = repository.create_calendar("Work")
    other = repository.create_calendar("Other")
    inside = repository.create_event(make_draft(calendar.id, summary="inside"))
    overlapping = repository.create_event(
        make_draft(
            calendar.id,
            summary="overlap",
            start_utc=4_200,
            end_utc=5_000,
            duration_seconds=800,
        )
    )
    repository.create_event(
        make_draft(
            calendar.id,
            summary="outside",
            start_utc=9_000,
            end_utc=10_000,
            duration_seconds=1_000,
        )
    )
    repository.create_event(make_draft(calendar.id, summary="recurring", rrule="FREQ=DAILY"))
    deleted = repository.create_event(make_draft(calendar.id, summary="deleted"))
    repository.delete_event(deleted.id)
    all_day = repository.create_event(
        make_draft(
            calendar.id,
            summary="all-day",
            start_utc=1_100,
            end_utc=86_400,
            start_local=None,
            start_tz=None,
            duration_seconds=None,
            start_date="2026-03-02",
            end_date="2026-03-03",
            is_all_day=True,
        )
    )
    repository.create_event(make_draft(other.id, summary="other calendar"))

    events = repository.list_events(calendar.id, 0, 5_000)

    assert [event.id for event in events] == [inside.id, all_day.id, overlapping.id]
    assert events[0].attendees == ()


def test_list_events_rejects_an_empty_range(repository: Repository) -> None:
    with pytest.raises(ValueError, match="earlier"):
        repository.list_events("cal_any", 10, 10)


def test_list_calendars_excludes_deleted(repository: Repository) -> None:
    work = repository.create_calendar("Work")
    repository.create_calendar("Home")
    repository.connection.execute(
        "UPDATE calendars SET deleted_utc = created_utc WHERE id = ?", (work.id,)
    )

    assert [calendar.name for calendar in repository.list_calendars()] == ["Home"]


def test_writes_set_change_context(repository: Repository) -> None:
    calendar = repository.create_calendar("Work", actor=PROFILE, batch_id="batch-1")

    calendar_log = repository.connection.execute(
        "SELECT * FROM change_log WHERE entity_id = ? ORDER BY seq DESC LIMIT 1",
        (calendar.id,),
    ).fetchone()
    assert calendar_log is not None
    assert calendar_log["origin"] == "local"
    assert calendar_log["actor"] == PROFILE
    assert calendar_log["batch_id"] == "batch-1"

    event = repository.create_event(
        make_draft(calendar.id),
        origin="system",
        actor="migrator",
        batch_id="batch-2",
    )
    event_log = repository.connection.execute(
        """
        SELECT * FROM change_log
         WHERE entity_id = ? AND op = 'insert'
         ORDER BY seq DESC
         LIMIT 1
        """,
        (event.id,),
    ).fetchone()
    assert event_log is not None
    assert event_log["origin"] == "system"
    assert event_log["pushed_utc"] is not None
    assert event_log["actor"] == "migrator"


def test_failed_write_rolls_back_and_repository_stays_usable(
    repository: Repository,
) -> None:
    calendar = repository.create_calendar("Work")

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_event(
            make_draft(calendar.id, id="broken", start_tz=None, is_floating=False)
        )

    assert repository.get_event("broken") is None
    assert repository.create_event(make_draft(calendar.id)).rev == 1


def test_global_store_has_one_calendar_and_owner(
    global_repository: Repository,
) -> None:
    calendars = global_repository.list_calendars()

    assert [calendar.id for calendar in calendars] == [GLOBAL_CALENDAR_ID]
    assert calendars[0].scope == "global"
    with pytest.raises(sqlite3.IntegrityError):
        global_repository.create_calendar("Second")

    event = global_repository.create_event(make_draft(GLOBAL_CALENDAR_ID), actor=PROFILE)

    assert event.scope == "global"
    assert event.owner_profile_id == PROFILE


def test_two_connections_write_the_global_store_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / PROFILE
    monkeypatch.setenv("HERMES_HOME", str(home))

    def writer(actor: str) -> None:
        connection = open_global_store()
        try:
            repository = Repository(connection)
            for index in range(5):
                repository.create_event(
                    make_draft(
                        GLOBAL_CALENDAR_ID,
                        summary=f"{actor}-{index}",
                        start_utc=1_000 + index,
                        end_utc=4_600 + index,
                    ),
                    actor=actor,
                )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, actor) for actor in ("coding", "local")]
        for future in futures:
            future.result()

    connection = open_global_store()
    try:
        repository = Repository(connection)
        events = repository.list_events(GLOBAL_CALENDAR_ID, 0, 100_000)
        assert len(events) == 10
        assert {event.owner_profile_id for event in events} == {"coding", "local"}
        rows = connection.execute(
            "SELECT seq FROM change_log WHERE entity_type = 'event' AND op = 'insert'"
        ).fetchall()
        assert len(rows) == 10
        assert len({int(row["seq"]) for row in rows}) == 10
    finally:
        connection.close()


def test_global_rev_conflict_between_two_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "root" / "profiles" / PROFILE
    monkeypatch.setenv("HERMES_HOME", str(home))
    first_connection = open_global_store()
    second_connection = open_global_store()
    first = Repository(first_connection)
    second = Repository(second_connection)
    try:
        event = first.create_event(make_draft(GLOBAL_CALENDAR_ID), actor="coding")
        barrier = threading.Barrier(2)

        def attempt(repository: Repository, summary: str) -> str:
            barrier.wait()
            try:
                repository.update_event(replace(event, summary=summary), expected_rev=event.rev)
            except RevisionConflict:
                return "conflict"
            return "updated"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(attempt, first, "one"),
                pool.submit(attempt, second, "two"),
            ]
            outcomes = sorted(future.result() for future in futures)

        assert outcomes == ["conflict", "updated"]
        stored = first.get_event(event.id)
        assert stored is not None
        assert stored.rev == event.rev + 1
        assert stored.summary in {"one", "two"}
    finally:
        first_connection.close()
        second_connection.close()
