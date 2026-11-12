"""Cross-profile global-calendar duplicate prevention and lease takeover."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_calendar.db import Repository, coordination
from hermes_calendar.db.reminders import ReminderStore
from tests.unit.reminder_support import (
    FakeClock,
    FakeRunner,
    create_calendar,
    create_timed_event,
    enable_chat,
    global_store,
    make_engine,
    profile_store,
)

BASE = 1_800_000_000


def _global_calendar(connection: sqlite3.Connection):  # type: ignore[no-untyped-def]
    calendars = Repository(connection).list_calendars()
    assert calendars
    return calendars[0]


def test_only_one_profile_delivers_and_takeover_does_not_replay(tmp_path: Path) -> None:
    shared = global_store(tmp_path)
    alice = profile_store(tmp_path, "alice")
    bob = profile_store(tmp_path, "bob")
    try:
        create_timed_event(shared, _global_calendar(shared), start_utc=BASE + 600)
        enable_chat(alice)
        enable_chat(bob)
        alice_runner = FakeRunner()
        bob_runner = FakeRunner()
        clock = FakeClock(BASE)
        alice_engine = make_engine(
            alice, clock=clock, runner=alice_runner, global_connection=shared
        )
        bob_engine = make_engine(
            bob,
            profile_id="bob",
            clock=clock,
            runner=bob_runner,
            global_connection=shared,
        )

        first = alice_engine.tick()
        second = bob_engine.tick()

        assert first.global_lease_holder is True
        assert first.delivered == 1
        assert second.global_lease_holder is False
        assert second.claimed == 0
        assert len(alice_runner.calls) == 1
        assert bob_runner.calls == []
        assert bob.execute("SELECT count(*) FROM reminder_instances").fetchone()[0] == 0

        # Kill alice's cron (no more ticks). Bob takes over after TTL + grace.
        clock.advance(300)
        takeover = bob_engine.tick()

        assert takeover.global_lease_holder is True
        assert takeover.duplicates_skipped == 1
        assert takeover.delivered == 0
        assert bob_runner.calls == []
        assert len(alice_runner.calls) == 1
    finally:
        bob.close()
        alice.close()
        shared.close()


def test_concurrent_global_ticks_produce_one_delivery(tmp_path: Path) -> None:
    shared = global_store(tmp_path)
    alice = profile_store(tmp_path, "alice")
    bob = profile_store(tmp_path, "bob")
    try:
        create_timed_event(shared, _global_calendar(shared), start_utc=BASE + 600)
        enable_chat(alice)
        enable_chat(bob)
        runner = FakeRunner()
        clock = FakeClock(BASE)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda engine: engine.tick(),
                    (
                        make_engine(
                            alice,
                            clock=clock,
                            runner=runner,
                            global_connection=shared,
                        ),
                        make_engine(
                            bob,
                            profile_id="bob",
                            clock=clock,
                            runner=runner,
                            global_connection=shared,
                        ),
                    ),
                )
            )

        assert sum(result.delivered for result in results) == 1
        assert len(runner.calls) == 1
    finally:
        bob.close()
        alice.close()
        shared.close()


def test_ledger_unavailable_falls_back_to_at_most_once(tmp_path: Path) -> None:
    shared = global_store(tmp_path)
    alice = profile_store(tmp_path)
    try:
        create_timed_event(shared, _global_calendar(shared), start_utc=BASE + 600)
        enable_chat(alice)
        shared.execute("DROP TABLE reminder_delivery_ledger")
        runner = FakeRunner()
        engine = make_engine(alice, clock=FakeClock(BASE), runner=runner, global_connection=shared)

        result = engine.tick()

        assert result.global_lease_holder is True
        assert result.suppressed == 1
        assert runner.calls == []
        row = alice.execute("SELECT state FROM reminder_instances").fetchone()
        assert row is not None
        assert row["state"] == "suppressed"
    finally:
        alice.close()
        shared.close()


def test_global_coordination_failure_degrades_but_profile_still_delivers(
    tmp_path: Path,
) -> None:
    shared = global_store(tmp_path)
    alice = profile_store(tmp_path)
    try:
        shared.execute("DROP TABLE reminder_lease")
        calendar = create_calendar(alice)
        create_timed_event(alice, calendar, start_utc=BASE + 600)
        enable_chat(alice)
        runner = FakeRunner()
        engine = make_engine(alice, clock=FakeClock(BASE), runner=runner, global_connection=shared)

        result = engine.tick()

        assert result.global_error is not None
        assert result.global_lease_holder is False
        assert result.delivered == 1
        assert len(runner.calls) == 1
    finally:
        alice.close()
        shared.close()


def test_lease_acquire_renew_and_takeover(tmp_path: Path) -> None:
    shared = global_store(tmp_path)
    try:
        assert coordination.read_lease(shared) is None
        assert coordination.acquire_or_renew_lease(shared, profile_id="alice", now=BASE)
        lease = coordination.read_lease(shared)
        assert lease is not None
        assert lease.owner_profile_id == "alice"
        assert lease.lease_expires_utc == BASE + 180

        assert coordination.acquire_or_renew_lease(shared, profile_id="alice", now=BASE + 60)
        lease = coordination.read_lease(shared)
        assert lease is not None
        assert lease.lease_expires_utc == BASE + 240

        assert not coordination.acquire_or_renew_lease(shared, profile_id="bob", now=BASE + 299)
        assert coordination.acquire_or_renew_lease(shared, profile_id="bob", now=BASE + 301)
        lease = coordination.read_lease(shared)
        assert lease is not None
        assert lease.owner_profile_id == "bob"

        assert not coordination.ledger_contains(shared, "missing")
        coordination.record_ledger(shared, dedup_key="abc", now=BASE, profile_id="alice")
        coordination.record_ledger(shared, dedup_key="abc", now=BASE, profile_id="bob")
        assert coordination.ledger_contains(shared, "abc")
        assert shared.execute("SELECT count(*) FROM reminder_delivery_ledger").fetchone()[0] == 1
    finally:
        shared.close()


def test_global_event_is_not_materialized_by_a_non_holder(tmp_path: Path) -> None:
    shared = global_store(tmp_path)
    alice = profile_store(tmp_path, "alice")
    bob = profile_store(tmp_path, "bob")
    try:
        create_timed_event(shared, _global_calendar(shared), start_utc=BASE + 600)
        clock = FakeClock(BASE)
        alice_engine = make_engine(
            alice, clock=clock, runner=FakeRunner(), global_connection=shared
        )
        bob_engine = make_engine(
            bob,
            profile_id="bob",
            clock=clock,
            runner=FakeRunner(),
            global_connection=shared,
        )
        alice_engine.tick()
        bob_engine.tick()
        assert ReminderStore(alice).list_instances()[0].store_id == "global"
        assert ReminderStore(bob).list_instances() == []
    finally:
        bob.close()
        alice.close()
        shared.close()
