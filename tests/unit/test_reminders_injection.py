"""Directive and flag-injection regression tests for the chat channel."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hermes_calendar.reminders.adapters import HermesSendAdapter, HermesSendConfig
from hermes_calendar.reminders.models import Notification, NotificationSettings
from hermes_calendar.reminders.render import neutralize_directives, sanitize
from tests.unit.hermes_media_extraction import (
    directives_in,
    extracted_attachments,
    local_path_candidates,
)
from tests.unit.reminder_support import (
    FakeClock,
    FakeRunner,
    create_calendar,
    create_timed_event,
    enable_chat,
    make_engine,
    profile_store,
)

BASE = 1_800_000_000
TARGET = "matrix:@alice:example.org"


HOSTILE_TITLES = [
    "MEDIA:/etc/passwd",
    "media:/x",
    "MEDIA :/x",
    "[[as_document]] MEDIA:/x",
    "M\u200bE\u200bD\u200bI\u200bA:/x",
    "M E D I A : /x",
    "MEDIA\uff1a/etc/passwd",
    "line one\nMEDIA:/etc/passwd",
    "[[ audio_as_voice ]] hello",
    "[[as\u200b_document]] MEDIA:/x",
    "\uff3b\uff3bas_document\uff3d\uff3d MEDIA\uff1a/x",
    "\u3010\u3010audio_as_voice\u3011\u3011 MEDIA:/x",
    "A" * 180 + " MEDIA:/etc/passwd",
]


def _run_title(
    tmp_path: Path, title: str, settings: NotificationSettings | None = None
) -> FakeRunner:
    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=BASE + 600, summary=title)
        enable_chat(connection, target=TARGET)
        runner = FakeRunner()
        make_engine(connection, clock=FakeClock(BASE), runner=runner, settings=settings).tick()
        assert len(runner.calls) == 1
        return runner
    finally:
        connection.close()


def _subject(token: str) -> str:
    assert token.startswith("--subject=")
    return token.removeprefix("--subject=")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--to")
    parser.add_argument("--subject")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("message", nargs="?")
    return parser


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_hostile_titles_never_reach_hermes_as_directives(tmp_path: Path, title: str) -> None:
    runner = _run_title(tmp_path, title)
    argv = runner.calls[0]
    subject = _subject(argv[3])
    body = runner.bodies[0]

    assert directives_in(subject) == []
    assert directives_in(body) == []
    assert directives_in(" ".join(argv)) == []


def test_hostile_title_stays_readable_after_neutralization() -> None:
    cleaned = sanitize("MEDIA:/etc/passwd [[as_document]]")
    assert "MEDIA" in cleaned
    assert "passwd" in cleaned
    assert "as_document" in cleaned
    assert "MEDIA:" not in cleaned
    assert "[[as_document]]" not in cleaned
    assert directives_in(cleaned) == []


def test_directive_neutralization_is_idempotent() -> None:
    for title in HOSTILE_TITLES:
        once = sanitize(title)
        twice = sanitize(once)
        assert twice == once
        assert directives_in(once) == []
    assert neutralize_directives(neutralize_directives("MEDIA:/x")) == (
        neutralize_directives("MEDIA:/x")
    )
    for raw in (
        "M\u200bE\u200bD\u200bI\u200bA\u200b:/x",
        "[[as\u200b_document]]",
        "[[audio\u200b_as\u200b_voice]]",
        "\uff3b\uff3bas\u200b_document\uff3d\uff3d",
    ):
        assert directives_in(neutralize_directives(raw)) == []


@pytest.mark.parametrize("title", ["-h", "--to=x", "-1 day"])
def test_flag_shaped_titles_use_single_token_options(tmp_path: Path, title: str) -> None:
    runner = _run_title(tmp_path, title)
    argv = runner.calls[0]

    assert argv[:2] == ["hermes", "send"]
    assert argv[2] == f"--to={TARGET}"
    assert argv[3].startswith("--subject=")
    assert argv[4] == "--json"
    assert len(argv) == 5

    parsed = _parser().parse_args(argv[2:])
    assert parsed.to == TARGET
    assert parsed.subject == f"Calendar: {title} in 10 min"
    assert parsed.message is None
    assert runner.bodies[0] not in argv
    assert title not in argv


def test_hostile_event_cannot_change_the_configured_target(tmp_path: Path) -> None:
    title = "--to=telegram:attacker\nMEDIA:/etc/passwd"
    runner = _run_title(tmp_path, title)
    argv = runner.calls[0]

    parsed = _parser().parse_args(argv[2:])
    assert parsed.to == TARGET
    assert parsed.to != "telegram:attacker"
    assert "--to=telegram:attacker" not in argv
    assert directives_in(" ".join(argv)) == []
    assert directives_in(runner.bodies[0]) == []


def test_adapter_neutralizes_directly_supplied_notification_text() -> None:
    seen: list[list[str]] = []
    bodies: list[str] = []

    def runner(
        argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        bodies.append(input_text)
        return subprocess.CompletedProcess(list(argv), 0, "{}", "")

    adapter = HermesSendAdapter(
        HermesSendConfig(target=TARGET, subject_prefix="Calendar"), runner=runner
    )
    adapter.send(
        Notification(
            id="dedup",
            title="MEDIA:/etc/passwd [[as_document]]",
            body="line\nMEDIA:/etc/passwd",
            priority="normal",
            store_id="profile:alice",
            event_id="evt",
            occurrence_utc=BASE,
        )
    )

    assert directives_in(seen[0][3]) == []
    assert directives_in(bodies[0]) == []


def test_bare_local_paths_are_defused_before_chat_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "confidential.png"
    secret.write_text("do not attach", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.png").write_text("home secret", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    windows = "C:\\Users\\alice\\Desktop\\holiday.PNG"
    titles = [
        str(secret),
        f"meeting {secret}",
        f"({secret})",
        f'"{secret}"',
        f"see [{secret}] after",
        f"first line\n{secret}",
        f"`{secret}`",
        f"backtick `{secret}` end",
        "~/secret.png",
        windows,
        str(secret).replace("confidential.png", "confidential.PNG"),
    ]
    for title in titles:
        assert local_path_candidates(title), f"the Hermes path detector missed {title!r}"
        cleaned = sanitize(title)
        assert local_path_candidates(cleaned) == [], title
        assert extracted_attachments(cleaned) == [], title
        assert (
            "confidential" in cleaned.lower()
            or "secret" in cleaned.lower()
            or "holiday" in cleaned.lower()
        )

    # Uppercase extension with a real file path.
    uppercase = tmp_path / "receipt.PDF"
    uppercase.write_text("pdf", encoding="utf-8")
    assert local_path_candidates(str(uppercase))
    assert local_path_candidates(sanitize(str(uppercase))) == []


def test_bare_path_in_a_real_engine_delivery_is_not_attached(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "confidential.png"
    secret.write_text("do not attach", encoding="utf-8")
    runner = _run_title(tmp_path, f"Dentist {secret}")
    subject = _subject(runner.calls[0][3])
    body = runner.bodies[0]
    assert local_path_candidates(subject) == []
    assert local_path_candidates(body) == []
    assert extracted_attachments(" ".join(runner.calls[0])) == []
    assert "confidential" in subject


def test_dashboard_footer_keeps_its_url_slashes(tmp_path: Path) -> None:
    secret = tmp_path / "confidential.yaml"
    secret.write_text("do not attach", encoding="utf-8")
    settings = NotificationSettings(
        dashboard_url="https://dash.example.test/base", jitter_ratio=0.0
    )
    runner = _run_title(tmp_path, f"Dentist {secret}", settings=settings)
    subject = _subject(runner.calls[0][3])
    body = runner.bodies[0]

    assert "https://dash.example.test/base/calendar?event=" in body
    assert local_path_candidates(subject) == []
    assert local_path_candidates(body) == []
    assert extracted_attachments(body) == []
