"""Route tests for the dashboard FastAPI backend.

The tests use FastAPI's TestClient with temporary ``HERMES_HOME`` stores, so they
exercise the same service layer the Hermes tools use and need no dashboard
process. FastAPI and httpx are development dependencies only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_calendar.db import StoreScopeError
from hermes_calendar.ics import MAX_ICS_BYTES
from hermes_calendar.service import InvalidArgument
from hermes_calendar.ui import dashboard_api

PROFILE = "work"
PREFIX = "/api/plugins/hermes-calendar"
ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:dashboard-test@example\r\n"
    "SUMMARY:Imported event\r\n"
    "DTSTART:20260302T090000Z\r\n"
    "DTEND:20260302T100000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture
def hidden_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point profile and global store resolution at a fresh temporary root."""
    home = tmp_path / "root" / "profiles" / PROFILE
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    return home


@pytest.fixture
def client(hidden_home: Path) -> TestClient:
    """Mount the plugin router the way the dashboard does."""
    del hidden_home
    app = FastAPI()
    app.include_router(dashboard_api.router, prefix=PREFIX)
    return TestClient(app)


def payload(response: Any) -> dict[str, Any]:
    """Decode a JSON response and assert it is an object."""
    decoded: Any = response.json()
    assert isinstance(decoded, dict)
    return cast(dict[str, Any], decoded)


def create_calendar(client: TestClient, *, name: str = "Work", scope: str = "profile") -> str:
    """Create a calendar through the API and return its ID."""
    response = client.post(PREFIX + "/calendar", json={"name": name, "scope": scope})
    assert response.status_code == 200, response.text
    body = payload(response)
    assert body["ok"] is True
    return cast(str, body["calendar"]["id"])


def create_event(client: TestClient, calendar_id: str, **overrides: Any) -> dict[str, Any]:
    """Create a standard event and return its stored payload."""
    body: dict[str, Any] = {
        "calendar_id": calendar_id,
        "summary": "Weekly review",
        "start": "2026-03-02T09:00:00",
        "end": "2026-03-02T10:00:00",
        "timezone": "America/New_York",
    }
    body.update(overrides)
    response = client.post(PREFIX + "/events", json=body)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], payload(response)["event"])


def list_events(
    client: TestClient, calendar_id: str, *, scope: str = "profile", **overrides: str
) -> dict[str, Any]:
    """List one calendar's events over the fixture's default range."""
    params: dict[str, str] = {
        "scope": scope,
        "calendar_id": calendar_id,
        "start": "2026-03-01T00:00:00",
        "end": "2026-04-01T00:00:00",
        "timezone": "America/New_York",
    }
    params.update(overrides)
    response = client.get(PREFIX + "/events", params=params)
    assert response.status_code == 200, response.text
    return payload(response)


# ----------------------------------------------------------------------- status


def test_version_reports_the_stable_api_contract(client: TestClient) -> None:
    response = client.get(PREFIX + "/version")

    assert response.status_code == 200
    body = payload(response)
    assert body["ok"] is True
    assert body["api_version"] == "1"
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "plugin.yaml").read_text(encoding="utf-8")
    )
    assert body["plugin_version"] == manifest["version"]


def test_status_reuses_calendar_status_and_reports_the_shared_store(
    client: TestClient, hidden_home: Path
) -> None:
    del hidden_home
    response = client.get(PREFIX + "/status")

    assert response.status_code == 200
    body = payload(response)
    assert body["ok"] is True
    assert body["plugin"] == "hermes-calendar"
    assert body["version"]
    assert "profile_data_dir" in body
    assert "global_data_dir" in body
    assert body["databases"]["profile"]["exists"] is False
    assert set(body["shared_store"]) == {"ok", "shared", "peers_seen", "writable", "detail"}


def test_status_never_exposes_a_raw_exception(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(_args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        raise RuntimeError("secret event text")

    monkeypatch.setattr(dashboard_api, "calendar_status", fail)
    caplog.set_level(logging.WARNING, logger=dashboard_api.__name__)

    response = client.get(PREFIX + "/status")

    assert response.status_code == 500
    body = payload(response)
    assert body == {"ok": False, "error": "internal error", "code": "internal_error"}
    assert "secret event text" not in caplog.text


# -------------------------------------------------------------------- calendars


def test_calendars_are_scoped_and_the_global_store_has_one(
    client: TestClient,
) -> None:
    empty = client.get(PREFIX + "/calendars", params={"scope": "profile"})
    assert empty.status_code == 200
    assert payload(empty) == {"ok": True, "scope": "profile", "count": 0, "calendars": []}

    calendar_id = create_calendar(client)
    listed = payload(client.get(PREFIX + "/calendars", params={"scope": "profile"}))
    assert listed["count"] == 1
    assert listed["calendars"][0]["id"] == calendar_id

    global_list = payload(client.get(PREFIX + "/calendars", params={"scope": "global"}))
    assert global_list["count"] == 1
    assert global_list["calendars"][0]["id"] == "cal_global"

    conflict = client.post(PREFIX + "/calendar", json={"name": "Second", "scope": "global"})
    assert conflict.status_code == 409
    assert payload(conflict)["code"] == "conflict"


def test_calendar_request_validation(client: TestClient) -> None:
    missing = client.post(PREFIX + "/calendar", json={"scope": "profile"})
    assert missing.status_code == 400
    assert payload(missing)["code"] == "invalid_argument"

    unknown = client.post(PREFIX + "/calendar", json={"name": "Work", "secret": "value"})
    assert unknown.status_code == 400
    assert "secret" in payload(unknown)["error"]

    long_name = client.post(PREFIX + "/calendar", json={"name": "x" * 201})
    assert long_name.status_code == 400

    bad_bool = client.post(PREFIX + "/calendar", json={"name": "Work", "read_only": "yes"})
    assert bad_bool.status_code == 400

    bad_scope = client.post(PREFIX + "/calendar", json={"name": "Work", "scope": "other"})
    assert bad_scope.status_code == 400


def test_query_scope_and_length_are_validated(client: TestClient) -> None:
    bad = client.get(PREFIX + "/calendars", params={"scope": "shared"})
    assert bad.status_code == 400
    assert payload(bad)["code"] == "invalid_argument"

    too_long = client.get(PREFIX + "/calendars", params={"scope": "x" * 17})
    assert too_long.status_code == 400

    no_scope = client.get(PREFIX + "/calendars")
    assert no_scope.status_code == 200
    assert payload(no_scope)["scope"] == "profile"


# ----------------------------------------------------------------------- events


def test_create_and_list_events(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)

    assert event["summary"] == "Weekly review"
    assert event["scope"] == "profile"
    assert event["start"]["utc"] == "2026-03-02T14:00:00Z"

    listed = list_events(client, calendar_id)
    assert listed["count"] == 1
    assert listed["range"]["timezone"] == "America/New_York"
    assert listed["events"][0]["master_id"] == event["id"]
    assert listed["events"][0]["master_rev"] == event["rev"]


def test_create_all_day_event(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(
        client,
        calendar_id,
        summary="Holiday",
        start="2026-03-02",
        end="2026-03-03",
        all_day=True,
    )

    assert event["all_day"] is True
    assert event["start"]["date"] == "2026-03-02"
    assert event["end"]["date"] == "2026-03-03"

    listed = list_events(client, calendar_id)
    assert listed["events"][0]["all_day"] is True
    assert listed["events"][0]["occurrence_start"]["date"] == "2026-03-02"


def test_recurring_events_expand_and_carry_master_revision(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    master = create_event(
        client,
        calendar_id,
        rrule="FREQ=WEEKLY;COUNT=3",
        start="2026-03-02T09:00:00",
        end="2026-03-02T10:00:00",
    )

    listed = list_events(client, calendar_id)

    assert listed["recurrence_expanded"] is True
    assert listed["count"] == 3
    assert {item["master_id"] for item in listed["events"]} == {master["id"]}
    assert all(item["master_rev"] == master["rev"] for item in listed["events"])
    assert all(item["master_rrule"] == "FREQ=WEEKLY;COUNT=3" for item in listed["events"])


def test_event_request_validation(client: TestClient) -> None:
    calendar_id = create_calendar(client)

    unknown = client.post(
        PREFIX + "/events", json={"calendar_id": calendar_id, "summary": "x", "bogus": 1}
    )
    assert unknown.status_code == 400
    assert "bogus" in payload(unknown)["error"]

    too_long = client.post(
        PREFIX + "/events", json={"calendar_id": calendar_id, "summary": "x" * 501}
    )
    assert too_long.status_code == 400

    bad_all_day = client.post(
        PREFIX + "/events",
        json={
            "calendar_id": calendar_id,
            "summary": "x",
            "start": "2026-03-02",
            "end": "2026-03-03",
            "all_day": "yes",
        },
    )
    assert bad_all_day.status_code == 400

    bad_recurrence = client.post(
        PREFIX + "/events",
        json={
            "calendar_id": calendar_id,
            "summary": "x",
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T10:00:00",
            "rdates": "20260302",
        },
    )
    assert bad_recurrence.status_code == 400

    too_many_dates = client.post(
        PREFIX + "/events",
        json={
            "calendar_id": calendar_id,
            "summary": "x",
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T10:00:00",
            "exdates": ["2026-03-02"] * (dashboard_api.MAX_RECURRENCE_VALUES + 1),
        },
    )
    assert too_many_dates.status_code == 400

    long_date = client.post(
        PREFIX + "/events",
        json={
            "calendar_id": calendar_id,
            "summary": "x",
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T10:00:00",
            "exdates": ["x" * (dashboard_api.MAX_RECURRENCE_VALUE_LENGTH + 1)],
        },
    )
    assert long_date.status_code == 400


def test_event_body_limits_and_shape(client: TestClient) -> None:
    calendar_id = create_calendar(client)

    invalid_json = client.post(
        PREFIX + "/events",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert invalid_json.status_code == 400
    assert payload(invalid_json)["code"] == "invalid_argument"

    not_object = client.post(
        PREFIX + "/events",
        content=b"[]",
        headers={"content-type": "application/json"},
    )
    assert not_object.status_code == 400

    empty = client.post(
        PREFIX + "/events", content=b"", headers={"content-type": "application/json"}
    )
    assert empty.status_code == 400

    too_many_fields: dict[str, Any] = {"calendar_id": calendar_id, "summary": "x"}
    too_many_fields.update({f"field{index}": 1 for index in range(dashboard_api.MAX_BODY_FIELDS)})
    too_many = client.post(PREFIX + "/events", json=too_many_fields)
    assert too_many.status_code == 400

    oversized = client.post(
        PREFIX + "/events",
        content=b"{}" + b" " * dashboard_api.MAX_JSON_BYTES,
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    assert payload(oversized)["code"] == "payload_too_large"

    bad_length = client.post(
        PREFIX + "/events",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": "not-a-number"},
    )
    assert bad_length.status_code == 400

    negative_length = client.post(
        PREFIX + "/events",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": "-1"},
    )
    assert negative_length.status_code == 400


def test_event_listing_validation_and_not_found(client: TestClient) -> None:
    missing_start = client.get(PREFIX + "/events", params={"end": "2026-03-02T00:00:00Z"})
    assert missing_start.status_code == 400

    missing_end = client.get(PREFIX + "/events", params={"start": "2026-03-02T00:00:00Z"})
    assert missing_end.status_code == 400

    reverse = client.get(
        PREFIX + "/events",
        params={"start": "2026-03-03T00:00:00Z", "end": "2026-03-02T00:00:00Z"},
    )
    assert reverse.status_code == 400

    bad_scope = client.get(
        PREFIX + "/events",
        params={"start": "2026-03-01T00:00:00Z", "end": "2026-03-02T00:00:00Z", "scope": "x"},
    )
    assert bad_scope.status_code == 400

    long_calendar = client.get(
        PREFIX + "/events",
        params={
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-03-02T00:00:00Z",
            "calendar_id": "x" * (dashboard_api.MAX_ID_LENGTH + 1),
        },
    )
    assert long_calendar.status_code == 400

    unknown = client.get(
        PREFIX + "/events",
        params={
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-03-02T00:00:00Z",
            "calendar_id": "cal_missing",
        },
    )
    assert unknown.status_code == 404
    assert payload(unknown)["code"] == "not_found"


def test_get_event_returns_the_same_shape_as_create(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    created = create_event(client, calendar_id)

    response = client.get(PREFIX + "/events/" + created["id"])

    assert response.status_code == 200, response.text
    body = payload(response)
    assert body["ok"] is True
    assert body["scope"] == "profile"
    assert body["event"] == created
    assert set(body) == {"ok", "scope", "event"}


def test_get_event_uses_profile_and_global_scope(client: TestClient) -> None:
    profile_calendar = create_calendar(client)
    profile_event = create_event(client, profile_calendar)
    global_event = create_event(client, "cal_global", summary="Team offsite", scope="global")

    global_response = client.get(
        PREFIX + "/events/" + global_event["id"], params={"scope": "global"}
    )
    assert global_response.status_code == 200, global_response.text
    assert payload(global_response)["event"]["summary"] == "Team offsite"
    assert payload(global_response)["event"]["scope"] == "global"

    missing_elsewhere = client.get(PREFIX + "/events/" + global_event["id"])
    assert missing_elsewhere.status_code == 404
    assert payload(missing_elsewhere)["code"] == "not_found"

    missing_global = client.get(
        PREFIX + "/events/" + profile_event["id"], params={"scope": "global"}
    )
    assert missing_global.status_code == 404


def test_get_event_returns_the_recurring_master(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    created = create_event(
        client,
        calendar_id,
        rrule="FREQ=WEEKLY;COUNT=3",
        start="2026-03-02T09:00:00",
        end="2026-03-02T10:00:00",
    )

    response = client.get(PREFIX + "/events/" + created["id"])

    assert response.status_code == 200, response.text
    event = payload(response)["event"]
    assert event == created
    assert event["rrule"] == "FREQ=WEEKLY;COUNT=3"
    assert event["rev"] == 1
    assert event["parent_id"] is None


def test_get_event_returns_one_occurrence_with_master_revision(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    master = create_event(
        client,
        calendar_id,
        rrule="FREQ=WEEKLY;COUNT=3",
        start="2026-03-02T09:00:00",
        end="2026-03-02T10:00:00",
    )
    listed = list_events(client, calendar_id)
    occurrence = listed["events"][1]

    response = client.get(
        PREFIX + "/events/" + master["id"],
        params={"occurrence_start": occurrence["occurrence_start"]["local"]},
    )

    assert response.status_code == 200, response.text
    returned = payload(response)["event"]
    assert returned == occurrence
    assert returned["master_id"] == master["id"]
    assert returned["master_rev"] == master["rev"]
    assert returned["recurrence_key"] == occurrence["recurrence_key"]
    assert returned["is_override"] is False


def test_get_event_returns_an_edited_occurrence_override(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    master = create_event(
        client,
        calendar_id,
        rrule="FREQ=DAILY;COUNT=3",
        start="2026-03-02T09:00:00",
        end="2026-03-02T10:00:00",
    )
    occurrence = list_events(client, calendar_id)["events"][1]
    edited = client.patch(
        PREFIX + "/events/" + master["id"],
        json={
            "expected_rev": master["rev"],
            "occurrence_start": occurrence["occurrence_start"]["local"],
            "series_scope": "this",
            "summary": "One day only",
        },
    )
    assert edited.status_code == 200, edited.text

    response = client.get(
        PREFIX + "/events/" + master["id"],
        params={"occurrence_start": occurrence["occurrence_start"]["local"]},
    )

    assert response.status_code == 200, response.text
    returned = payload(response)["event"]
    assert returned["summary"] == "One day only"
    assert returned["is_override"] is True
    assert returned["override_event_id"] == payload(edited)["event"]["id"]
    assert returned["master_rev"] == payload(edited)["event"]["rev"]


def test_get_event_deleted_and_unknown_ids_are_not_found(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)
    deleted = client.delete(
        PREFIX + "/events/" + event["id"],
        params={"scope": "profile", "expected_rev": str(event["rev"])},
    )
    assert deleted.status_code == 200

    tombstone = client.get(PREFIX + "/events/" + event["id"])
    assert tombstone.status_code == 404
    assert payload(tombstone)["code"] == "not_found"

    unknown = client.get(PREFIX + "/events/missing-event")
    assert unknown.status_code == 404
    assert payload(unknown)["code"] == "not_found"


def test_get_event_rejects_bad_id_scope_and_occurrence(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)

    too_long = client.get(PREFIX + "/events/" + "x" * (dashboard_api.MAX_ID_LENGTH + 1))
    assert too_long.status_code == 400
    assert payload(too_long)["code"] == "invalid_argument"

    blank = client.get(PREFIX + "/events/%20")
    assert blank.status_code == 400
    assert payload(blank)["code"] == "invalid_argument"

    bad_scope = client.get(PREFIX + "/events/" + event["id"], params={"scope": "everyone"})
    assert bad_scope.status_code == 400
    assert payload(bad_scope)["code"] == "invalid_argument"

    long_occurrence = client.get(
        PREFIX + "/events/" + event["id"],
        params={"occurrence_start": "x" * (dashboard_api.MAX_TIME_LENGTH + 1)},
    )
    assert long_occurrence.status_code == 400

    not_an_occurrence = client.get(
        PREFIX + "/events/" + event["id"],
        params={"occurrence_start": "2026-03-03T09:00:00"},
    )
    assert not_an_occurrence.status_code == 400
    assert payload(not_an_occurrence)["code"] == "invalid_argument"


# ---------------------------------------------------------------------- updates


def test_update_requires_expected_rev_and_uses_compare_and_swap(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)
    url = PREFIX + "/events/" + event["id"]

    missing = client.patch(url, json={"summary": "Renamed"})
    assert missing.status_code == 400
    assert "expected_rev" in payload(missing)["error"]

    boolean = client.patch(url, json={"expected_rev": True, "summary": "Renamed"})
    assert boolean.status_code == 400

    updated = client.patch(url, json={"expected_rev": event["rev"], "summary": "Renamed"})
    assert updated.status_code == 200
    updated_event = payload(updated)["event"]
    assert updated_event["summary"] == "Renamed"
    assert updated_event["rev"] == event["rev"] + 1

    stale = client.patch(url, json={"expected_rev": event["rev"], "summary": "Stale"})
    assert stale.status_code == 409
    stale_body = payload(stale)
    assert stale_body["code"] == "revision_conflict"
    assert stale_body["event_id"] == event["id"]
    assert stale_body["expected_rev"] == event["rev"]


def test_update_rejects_unknown_and_overlong_fields(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)
    url = PREFIX + "/events/" + event["id"]

    unknown = client.patch(url, json={"expected_rev": event["rev"], "all_day": True})
    assert unknown.status_code == 400

    too_long = client.patch(url, json={"expected_rev": event["rev"], "summary": "x" * 501})
    assert too_long.status_code == 400

    empty_summary = client.patch(url, json={"expected_rev": event["rev"], "summary": " "})
    assert empty_summary.status_code == 400

    bad_scope = client.patch(url, json={"expected_rev": event["rev"], "scope": "everyone"})
    assert bad_scope.status_code == 400


def test_edit_one_occurrence_and_reject_bad_series_scope(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    master = create_event(
        client,
        calendar_id,
        rrule="FREQ=WEEKLY;COUNT=3",
        start="2026-03-02T09:00:00",
        end="2026-03-02T10:00:00",
    )
    listed = list_events(client, calendar_id)
    occurrence = listed["events"][1]

    response = client.patch(
        PREFIX + "/events/" + master["id"],
        json={
            "expected_rev": occurrence["master_rev"],
            "occurrence_start": occurrence["occurrence_start"]["local"],
            "series_scope": "this",
            "summary": "One week only",
        },
    )
    assert response.status_code == 200, response.text
    changed = payload(response)["event"]
    assert changed["recurrence_key"] == occurrence["recurrence_key"]
    assert changed["parent_id"] == master["id"]

    bad_scope = client.patch(
        PREFIX + "/events/" + master["id"],
        json={"expected_rev": occurrence["master_rev"], "series_scope": "some"},
    )
    assert bad_scope.status_code == 400


def test_all_day_series_date_edits_stay_unsupported(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(
        client,
        calendar_id,
        summary="Holiday",
        start="2026-03-02",
        end="2026-03-03",
        all_day=True,
    )

    response = client.patch(
        PREFIX + "/events/" + event["id"],
        json={"expected_rev": event["rev"], "start": "2026-03-04"},
    )

    assert response.status_code == 400
    assert payload(response)["code"] == "invalid_argument"


# ---------------------------------------------------------------------- deletes


def test_delete_requires_expected_rev_and_is_idempotent(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)
    url = PREFIX + "/events/" + event["id"]

    missing = client.delete(url, params={"scope": "profile"})
    assert missing.status_code == 400

    not_integer = client.delete(url, params={"scope": "profile", "expected_rev": "one"})
    assert not_integer.status_code == 400

    negative = client.delete(url, params={"scope": "profile", "expected_rev": "0"})
    assert negative.status_code == 400

    too_long = client.delete(url, params={"scope": "profile", "expected_rev": "1" * 17})
    assert too_long.status_code == 400

    deleted = client.delete(url, params={"scope": "profile", "expected_rev": str(event["rev"])})
    assert deleted.status_code == 200
    assert payload(deleted)["deleted"] is True

    again = client.delete(url, params={"scope": "profile", "expected_rev": str(event["rev"])})
    assert again.status_code == 200
    assert payload(again)["already_deleted"] is True


def test_delete_with_stale_revision_conflicts(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)
    updated = client.patch(
        PREFIX + "/events/" + event["id"],
        json={"expected_rev": event["rev"], "summary": "Renamed"},
    )
    assert updated.status_code == 200

    stale = client.delete(
        PREFIX + "/events/" + event["id"],
        params={"scope": "profile", "expected_rev": str(event["rev"])},
    )
    assert stale.status_code == 409
    assert payload(stale)["code"] == "revision_conflict"


def test_delete_one_occurrence(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    master = create_event(
        client,
        calendar_id,
        rrule="FREQ=DAILY;COUNT=3",
        start="2026-03-02T09:00:00",
        end="2026-03-02T10:00:00",
    )
    occurrence = list_events(client, calendar_id)["events"][1]

    response = client.delete(
        PREFIX + "/events/" + master["id"],
        params={
            "scope": "profile",
            "expected_rev": str(occurrence["master_rev"]),
            "series_scope": "this",
            "occurrence_start": occurrence["occurrence_start"]["local"],
        },
    )

    assert response.status_code == 200, response.text
    body = payload(response)
    assert body["deleted"] is True
    assert body["occurrence_start"] == occurrence["occurrence_start"]["local"]


# ------------------------------------------------------------------------- ICS


def test_import_and_export_ics(client: TestClient) -> None:
    calendar_id = create_calendar(client)

    imported = client.post(
        PREFIX + "/import",
        json={"scope": "profile", "calendar_id": calendar_id, "text": ICS},
    )
    assert imported.status_code == 200, imported.text
    assert payload(imported)["imported"] == 1

    again = client.post(
        PREFIX + "/import",
        json={"scope": "profile", "calendar_id": calendar_id, "text": ICS},
    )
    assert payload(again)["updated"] == 1

    exported = client.get(
        PREFIX + "/export",
        params={
            "scope": "profile",
            "calendar_id": calendar_id,
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",
        },
    )
    assert exported.status_code == 200
    body = payload(exported)
    assert body["count"] == 1
    assert "BEGIN:VCALENDAR" in body["ics"]
    assert "SUMMARY:Imported event" in body["ics"]


def test_import_reports_parser_warnings_without_raising(client: TestClient) -> None:
    calendar_id = create_calendar(client)

    response = client.post(
        PREFIX + "/import",
        json={"scope": "profile", "calendar_id": calendar_id, "text": "not a calendar"},
    )

    assert response.status_code == 200
    body = payload(response)
    assert body["imported"] == 0
    assert body["warnings"]


def test_import_size_caps(client: TestClient) -> None:
    calendar_id = create_calendar(client)

    oversized_text = client.post(
        PREFIX + "/import",
        json={
            "scope": "profile",
            "calendar_id": calendar_id,
            "text": "x" * (MAX_ICS_BYTES + 1),
        },
    )
    assert oversized_text.status_code == 413
    assert payload(oversized_text)["code"] == "payload_too_large"

    missing = client.post(
        PREFIX + "/import",
        json={"scope": "profile", "calendar_id": calendar_id},
    )
    assert missing.status_code == 400


def test_export_validation_and_not_found(client: TestClient) -> None:
    missing = client.get(
        PREFIX + "/export",
        params={"calendar_id": "cal_x", "start": "2026-03-01T00:00:00Z"},
    )
    assert missing.status_code == 400

    unknown = client.get(
        PREFIX + "/export",
        params={
            "calendar_id": "cal_missing",
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",
        },
    )
    assert unknown.status_code == 404

    bad_scope = client.get(
        PREFIX + "/export",
        params={
            "calendar_id": "cal_global",
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",
            "scope": "both",
        },
    )
    assert bad_scope.status_code == 400


# --------------------------------------------------------------- error mapping


class _FailingService:
    """Service stand-in whose every method raises the configured exception."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def list_calendars(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._error


def test_store_errors_are_sanitized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        dashboard_api, "_service", lambda: _FailingService(StoreScopeError("wrong store secret"))
    )
    caplog.set_level(logging.WARNING, logger=dashboard_api.__name__)

    response = client.get(PREFIX + "/calendars")

    assert response.status_code == 503
    body = payload(response)
    assert body["code"] == "store_error"
    assert "wrong store secret" not in body["error"]
    assert "wrong store secret" not in caplog.text


def test_integrity_errors_are_sanitized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dashboard_api,
        "_service",
        lambda: _FailingService(sqlite3.IntegrityError("UNIQUE secret value")),
    )

    response = client.get(PREFIX + "/calendars")

    assert response.status_code == 409
    body = payload(response)
    assert body["code"] == "conflict"
    assert "secret" not in body["error"]


def test_unexpected_errors_are_sanitized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        dashboard_api, "_service", lambda: _FailingService(RuntimeError("event text"))
    )
    caplog.set_level(logging.WARNING, logger=dashboard_api.__name__)

    response = client.get(PREFIX + "/calendars")

    assert response.status_code == 500
    assert payload(response) == {"ok": False, "error": "internal error", "code": "internal_error"}
    assert "event text" not in caplog.text


def test_service_errors_keep_their_code_and_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dashboard_api,
        "_service",
        lambda: _FailingService(InvalidArgument("summary is required")),
    )

    response = client.get(PREFIX + "/calendars")

    assert response.status_code == 400
    assert payload(response) == {
        "ok": False,
        "error": "summary is required",
        "code": "invalid_argument",
    }


def test_long_event_id_is_rejected_before_the_service(client: TestClient) -> None:
    response = client.patch(
        PREFIX + "/events/" + "x" * (dashboard_api.MAX_ID_LENGTH + 1),
        json={"expected_rev": 1, "summary": "x"},
    )
    assert response.status_code == 400
    assert "event_id" in payload(response)["error"]


def test_json_payload_is_deterministic_for_calendar_lists(client: TestClient) -> None:
    create_calendar(client)

    response = client.get(PREFIX + "/calendars", params={"scope": "profile"})

    decoded = json.loads(response.text)
    assert decoded["ok"] is True
    assert decoded["count"] == 1


def test_declared_content_length_over_limit_is_rejected(client: TestClient) -> None:
    response = client.post(
        PREFIX + "/events",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(dashboard_api.MAX_JSON_BYTES + 1),
        },
    )
    assert response.status_code == 413
    assert payload(response)["code"] == "payload_too_large"


def test_body_limit_without_content_length_is_still_enforced() -> None:
    class _Request:
        headers: dict[str, str] = {}

        async def body(self) -> bytes:
            return b"{}" + b" " * 32

    fake: Any = _Request()
    with pytest.raises(dashboard_api.PayloadTooLarge) as captured:
        asyncio.run(dashboard_api._read_json_body(fake, limit=16, allowed=frozenset()))

    assert captured.value.code == "payload_too_large"


def test_required_query_blank_and_field_type_errors(client: TestClient) -> None:
    blank_start = client.get(
        PREFIX + "/events",
        params={"start": "   ", "end": "2026-03-02T00:00:00Z"},
    )
    assert blank_start.status_code == 400

    calendar_id = create_calendar(client)
    wrong_type = client.post(
        PREFIX + "/events",
        json={
            "calendar_id": calendar_id,
            "summary": 42,
            "start": "2026-03-02T09:00:00",
            "end": "2026-03-02T10:00:00",
        },
    )
    assert wrong_type.status_code == 400

    event = create_event(client, calendar_id)
    low_revision = client.patch(
        PREFIX + "/events/" + event["id"],
        json={"expected_rev": 0, "summary": "x"},
    )
    assert low_revision.status_code == 400


def test_recurrence_value_type_and_empty_item_errors(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    base: dict[str, Any] = {
        "calendar_id": calendar_id,
        "summary": "x",
        "start": "2026-03-02T09:00:00",
        "end": "2026-03-02T10:00:00",
    }

    non_string = dict(base)
    non_string["rdates"] = [1]
    assert client.post(PREFIX + "/events", json=non_string).status_code == 400

    blank = dict(base)
    blank["exdates"] = ["  "]
    assert client.post(PREFIX + "/events", json=blank).status_code == 400


def test_update_accepts_every_documented_field(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(client, calendar_id)

    response = client.patch(
        PREFIX + "/events/" + event["id"],
        json={
            "expected_rev": event["rev"],
            "description": "Notes",
            "location": "Room 1",
            "url": "https://example.invalid/meeting",
            "timezone": "UTC",
            "start": "2026-03-02T08:00:00",
            "end": "2026-03-02T09:00:00",
            "rdates": ["2026-03-09T08:00:00"],
            "exdates": ["2026-03-16T08:00:00"],
        },
    )

    assert response.status_code == 200, response.text
    changed = payload(response)["event"]
    assert changed["description"] == "Notes"
    assert changed["location"] == "Room 1"
    assert changed["url"] == "https://example.invalid/meeting"
    assert changed["rdate"] == ["2026-03-09T08:00:00"]
    assert changed["exdate"] == ["2026-03-16T08:00:00"]


def test_update_can_clear_a_rule_and_description(client: TestClient) -> None:
    calendar_id = create_calendar(client)
    event = create_event(
        client,
        calendar_id,
        description="Notes",
        rrule="FREQ=DAILY;COUNT=2",
    )

    response = client.patch(
        PREFIX + "/events/" + event["id"],
        json={
            "expected_rev": event["rev"],
            "description": None,
            "rrule": None,
        },
    )

    assert response.status_code == 200, response.text
    changed = payload(response)["event"]
    assert changed["description"] is None
    assert changed["rrule"] is None


def test_ics_text_type_and_empty_errors(client: TestClient) -> None:
    calendar_id = create_calendar(client)

    not_text = client.post(
        PREFIX + "/import",
        json={"scope": "profile", "calendar_id": calendar_id, "text": 5},
    )
    assert not_text.status_code == 400

    empty = client.post(
        PREFIX + "/import",
        json={"scope": "profile", "calendar_id": calendar_id, "text": "   "},
    )
    assert empty.status_code == 400


def test_event_id_helpers_reject_blank_and_long_values() -> None:
    with pytest.raises(InvalidArgument):
        dashboard_api._path_id("   ")
    with pytest.raises(InvalidArgument):
        dashboard_api._path_id("x" * (dashboard_api.MAX_ID_LENGTH + 1))


def test_status_rejects_a_non_object_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard_api, "calendar_status", lambda _args=None, **_kw: "[]")

    response = client.get(PREFIX + "/status")

    assert response.status_code == 500
    assert payload(response)["code"] == "internal_error"
