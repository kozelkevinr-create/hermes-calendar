"""Dashboard notification-centre and reminder-setup routes.

The tests build real profile/global stores through ``HERMES_HOME``, let the
reminder engine materialize and deliver notifications with a fake clock, then
drive the FastAPI router with ``TestClient``. No ticker, cron job, or
``hermes send`` process is started.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_calendar.db import Repository, open_global_store, open_profile_store
from hermes_calendar.db.reminders import ReminderStore
from hermes_calendar.reminders import doctor
from hermes_calendar.reminders import service as reminder_service
from hermes_calendar.reminders.cron import cron_command, tick_script
from hermes_calendar.ui import dashboard_api
from tests.unit.reminder_support import (
    FakeClock,
    create_calendar,
    create_timed_event,
    make_engine,
)

PROFILE = "alice"
BASE = 1_800_000_000
PREFIX = "/api/plugins/hermes-calendar"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point profile and global stores at a fresh temporary Hermes root."""
    profile_home = tmp_path / "root" / "profiles" / PROFILE
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    return profile_home


@pytest.fixture
def client(home: Path) -> TestClient:
    """Mount the dashboard router exactly as Hermes does."""
    del home
    app = FastAPI()
    app.include_router(dashboard_api.router, prefix=PREFIX)
    return TestClient(app)


def payload(response: Any) -> dict[str, Any]:
    """Decode a JSON object response."""
    decoded: Any = response.json()
    assert isinstance(decoded, dict)
    return cast(dict[str, Any], decoded)


def seed_notifications(*, count: int = 5, global_event: bool = True) -> None:
    """Materialize ``count`` profile notifications (and one global one)."""
    profile = open_profile_store()
    shared = None
    try:
        calendar = create_calendar(profile)
        for index in range(count):
            create_timed_event(
                profile,
                calendar,
                start_utc=BASE + 600 + index * 60,
                summary=f"Profile {index + 1}",
            )
        if global_event:
            shared = open_global_store()
            global_calendar = Repository(shared).list_calendars()[0]
            create_timed_event(
                shared,
                global_calendar,
                start_utc=BASE + 600,
                summary="Shared standup",
            )
        make_engine(profile, clock=FakeClock(BASE + 600), global_connection=shared).tick()
    finally:
        if shared is not None:
            shared.close()
        profile.close()


def notification_ids(client: TestClient) -> list[str]:
    """Return every visible notification id through the API."""
    response = client.get(PREFIX + "/notifications", params={"limit": 100})
    assert response.status_code == 200, response.text
    body = payload(response)
    return [str(item["id"]) for item in body["notifications"]]


def store_prefs(**values: Any) -> None:
    """Write profile preferences without opening a second connection per value."""
    connection = open_profile_store()
    try:
        store = ReminderStore(connection)
        for key, value in values.items():
            store.set_pref(key, value)
    finally:
        connection.close()


# ------------------------------------------------------------------ listing


def test_notifications_lists_profile_and_global_titles(client: TestClient) -> None:
    seed_notifications()

    response = client.get(PREFIX + "/notifications")

    assert response.status_code == 200
    body = payload(response)
    assert body["ok"] is True
    assert body["count"] == 6
    assert body["total"] == 6
    assert body["limit"] == dashboard_api.DEFAULT_NOTIFICATION_LIMIT
    assert body["offset"] == 0
    titles = {str(item["title"]) for item in body["notifications"]}
    assert "Profile 1" in titles
    assert "Shared standup" in titles
    global_item = next(item for item in body["notifications"] if item["store_id"] == "global")
    assert global_item["title"] == "Shared standup"
    assert "description" not in global_item
    assert "location" not in global_item


def test_notifications_paginates_within_caps(client: TestClient) -> None:
    seed_notifications()

    first = payload(client.get(PREFIX + "/notifications", params={"limit": 2, "offset": 0}))
    second = payload(client.get(PREFIX + "/notifications", params={"limit": 2, "offset": 2}))
    last = payload(client.get(PREFIX + "/notifications", params={"limit": 2, "offset": 5}))
    beyond = payload(client.get(PREFIX + "/notifications", params={"limit": 2, "offset": 6}))

    assert (first["count"], first["total"], first["limit"], first["offset"]) == (2, 6, 2, 0)
    assert second["count"] == 2
    assert last["count"] == 1
    assert beyond["count"] == 0
    assert beyond["total"] == 6


def test_notifications_validates_pagination_and_filters(client: TestClient) -> None:
    seed_notifications()

    cases = (
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "many"},
        {"offset": "10001"},
        {"offset": "-1"},
        {"include_dismissed": "maybe"},
        {"state": "bogus"},
        {"state": "x" * 33},
    )
    for params in cases:
        response = client.get(PREFIX + "/notifications", params=params)
        assert response.status_code == 400, (params, response.text)
        assert payload(response)["code"] == "invalid_argument"

    by_state = payload(
        client.get(PREFIX + "/notifications", params={"state": "delivered", "limit": 100})
    )
    assert by_state["total"] == 6
    accepted_false = client.get(PREFIX + "/notifications", params={"include_dismissed": "0"})
    assert accepted_false.status_code == 200

    hidden = payload(client.get(PREFIX + "/notifications"))
    dismissed_id = notification_ids(client)[0]
    assert client.post(PREFIX + "/notifications/" + dismissed_id + "/dismiss").status_code == 200
    after = payload(client.get(PREFIX + "/notifications"))
    visible = payload(client.get(PREFIX + "/notifications", params={"include_dismissed": "true"}))
    assert after["total"] == hidden["total"] - 1
    assert visible["total"] == hidden["total"]


# ---------------------------------------------------------------- dismissal


def test_dismiss_unknown_notification_is_not_found(client: TestClient) -> None:
    response = client.post(PREFIX + "/notifications/not-a-real-id/dismiss")

    assert response.status_code == 404
    body = payload(response)
    assert body["code"] == "not_found"
    assert body["error_type"] == "NotificationNotFoundError"


def test_dismiss_is_idempotent_and_returns_the_notification(client: TestClient) -> None:
    seed_notifications(count=1, global_event=False)
    notification_id = notification_ids(client)[0]

    first = client.post(PREFIX + "/notifications/" + notification_id + "/dismiss")
    second = client.post(PREFIX + "/notifications/" + notification_id + "/dismiss")

    assert first.status_code == 200
    assert second.status_code == 200
    assert payload(first)["notification"]["state"] == "acknowledged"
    assert payload(second)["notification"]["state"] == "acknowledged"


def test_notification_id_length_is_validated(client: TestClient) -> None:
    response = client.post(
        PREFIX
        + "/notifications/"
        + "x" * (dashboard_api.MAX_NOTIFICATION_ID_LENGTH + 1)
        + "/dismiss"
    )

    assert response.status_code == 400
    assert "notification_id" in payload(response)["error"]


# ------------------------------------------------------------------- snooze


@pytest.mark.parametrize("minutes", [5, 15, 60, 1440])
def test_snooze_accepts_the_allowlist(client: TestClient, minutes: int) -> None:
    seed_notifications(count=1, global_event=False)
    notification_id = notification_ids(client)[0]

    response = client.post(
        PREFIX + "/notifications/" + notification_id + "/snooze",
        json={"minutes": minutes},
    )

    assert response.status_code == 200, response.text
    item = payload(response)["notification"]
    assert item["state"] == "scheduled"
    assert item["snooze_until"] is not None


def test_snooze_rejects_values_outside_the_allowlist(client: TestClient) -> None:
    seed_notifications(count=1, global_event=False)
    notification_id = notification_ids(client)[0]

    for minutes in (10, 0, 7, 1441):
        response = client.post(
            PREFIX + "/notifications/" + notification_id + "/snooze",
            json={"minutes": minutes},
        )
        assert response.status_code == 400, minutes
        assert payload(response)["code"] == "invalid_argument"

    missing = client.post(PREFIX + "/notifications/" + notification_id + "/snooze", json={})
    assert missing.status_code == 400

    string_minutes = client.post(
        PREFIX + "/notifications/" + notification_id + "/snooze",
        json={"minutes": "15"},
    )
    assert string_minutes.status_code == 400

    unknown_field = client.post(
        PREFIX + "/notifications/" + notification_id + "/snooze",
        json={"minutes": 15, "forever": True},
    )
    assert unknown_field.status_code == 400


def test_snooze_unknown_and_dismissed_notifications_are_not_found(client: TestClient) -> None:
    seed_notifications(count=1, global_event=False)
    notification_id = notification_ids(client)[0]

    unknown = client.post(PREFIX + "/notifications/not-a-real-id/snooze", json={"minutes": 15})
    assert unknown.status_code == 404

    assert client.post(PREFIX + "/notifications/" + notification_id + "/dismiss").status_code == 200
    dismissed = client.post(
        PREFIX + "/notifications/" + notification_id + "/snooze", json={"minutes": 15}
    )
    assert dismissed.status_code == 404
    assert payload(dismissed)["code"] == "not_found"


def test_snooze_works_for_a_global_calendar_notification(client: TestClient) -> None:
    seed_notifications(count=0, global_event=True)
    body = payload(client.get(PREFIX + "/notifications"))
    item = body["notifications"][0]
    assert item["store_id"] == "global"

    response = client.post(
        PREFIX + "/notifications/" + str(item["id"]) + "/snooze",
        json={"minutes": 60},
    )

    assert response.status_code == 200
    assert payload(response)["notification"]["state"] == "scheduled"


# --------------------------------------------------------------- doctor/status


def test_reminders_status_defaults_are_explicit_and_do_not_leak_targets(
    client: TestClient,
) -> None:
    response = client.get(PREFIX + "/reminders/status")

    assert response.status_code == 200
    body = payload(response)
    assert body["delivery_target_configured"] is False
    assert body["last_tick"] is None
    assert body["last_tick_age_seconds"] is None
    assert body["cron_appears_running"] is False
    assert body["in_process_ticker_enabled"] is False
    assert body["ticker_interval_seconds"] == 30
    assert body["cron_command"] == cron_command()
    assert body["tick_script"] == tick_script()
    assert body["job_name"] == "calendar reminders"
    assert body["schedule"] == "1m"
    assert any(check["name"] == "external_channel" for check in body["checks"])


def test_reminders_status_reports_setup_without_the_target_value(
    client: TestClient,
) -> None:
    target = "matrix:@private-room:example.org"
    connection = open_profile_store()
    try:
        ReminderStore(connection).set_channel("phone", enabled=True, config={"target": target})
        ReminderStore(connection).set_pref(
            "notifications",
            {"in_process_ticker": {"enabled": True, "interval_seconds": 45}},
        )
        ReminderStore(connection).set_pref("last_tick_utc", int(time.time()) - 30)
    finally:
        connection.close()

    response = client.get(PREFIX + "/reminders/status")

    assert response.status_code == 200
    body = payload(response)
    assert body["delivery_target_configured"] is True
    assert body["last_tick"] is not None
    assert 0 <= body["last_tick_age_seconds"] <= 180
    assert body["cron_appears_running"] is True
    assert body["in_process_ticker_enabled"] is True
    assert body["ticker_interval_seconds"] == 45
    assert target not in response.text
    assert "private-room" not in json.dumps(body)


def test_reminders_status_ignores_malformed_last_tick_and_stale_ticks(
    client: TestClient,
) -> None:
    store_prefs(last_tick_utc="not-an-integer")
    body = payload(client.get(PREFIX + "/reminders/status"))
    assert body["last_tick"] is None
    assert body["cron_appears_running"] is False

    store_prefs(last_tick_utc=int(time.time()) - 1_000)
    body = payload(client.get(PREFIX + "/reminders/status"))
    assert body["last_tick"] is not None
    assert body["cron_appears_running"] is False

    store_prefs(last_tick_utc=True)
    body = payload(client.get(PREFIX + "/reminders/status"))
    assert body["last_tick"] is None


def test_reminder_status_service_handles_a_missing_global_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    del client

    def failing_global() -> None:
        raise OSError("global unavailable")

    connection = open_profile_store()
    try:
        monkeypatch.setattr(reminder_service, "open_global_store", failing_global)
        report = doctor.reminder_status(connection, now=BASE, which=lambda _: "/bin/hermes")
    finally:
        connection.close()

    assert report["delivery_target_configured"] is False
    assert report["last_tick"] is None
    assert report["cron_appears_running"] is False


def test_reminder_status_service_uses_recent_ticks_and_configured_ticker(
    client: TestClient,
) -> None:
    del client
    connection = open_profile_store()
    try:
        store = ReminderStore(connection)
        store.set_channel("phone", enabled=True, config={"target": "matrix:@me"})
        store.set_pref("last_tick_utc", BASE - 30)
        store.set_pref("notifications", {"in_process_ticker": {"enabled": True}})
        report = doctor.reminder_status(connection, now=BASE, which=lambda _: "/bin/hermes")
    finally:
        connection.close()

    assert report["delivery_target_configured"] is True
    assert report["last_tick"] == "2027-01-15T07:59:30Z"
    assert report["last_tick_age_seconds"] == 30
    assert report["cron_appears_running"] is True
    assert report["in_process_ticker_enabled"] is True
    assert report["ok"] is True


def test_notification_service_rejects_an_unknown_state(home: Path) -> None:
    del home
    connection = open_profile_store()
    try:
        with pytest.raises(ValueError, match="state must be one of"):
            reminder_service.list_notifications(connection, state="bogus")
        with pytest.raises(ValueError, match="state must be one of"):
            reminder_service.count_notifications(connection, state="bogus")
    finally:
        connection.close()
