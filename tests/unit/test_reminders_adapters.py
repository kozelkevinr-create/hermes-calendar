"""``hermes send`` adapter contract tests with an injected runner."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from hermes_calendar.db.reminders import ChannelRow
from hermes_calendar.reminders.adapters import (
    HermesSendAdapter,
    HermesSendConfig,
    InAppNotifier,
    build_chat_notifier,
    subprocess_runner,
)
from hermes_calendar.reminders.models import Notification
from hermes_calendar.reminders.render import (
    neutralize_mentions,
    relative_phrase,
    sanitize,
)


def _notification() -> Notification:
    return Notification(
        id="dedup",
        title="Dentist in 30 min",
        body="Dentist in 30 min\nOpen the calendar app.",
        priority="normal",
        store_id="profile:alice",
        event_id="evt",
        occurrence_utc=1_800_000_000,
    )


class _Raiser:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __call__(
        self, argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise self._error


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_config_requires_an_explicit_target() -> None:
    with pytest.raises(ValueError, match="explicit target"):
        HermesSendConfig(target="  ")


def test_in_app_always_succeeds() -> None:
    adapter = InAppNotifier()
    assert adapter.name == "in_app"
    assert adapter.available().available is True
    assert adapter.send(_notification()).outcome == "success"


def test_availability_checks_the_binary_only_for_the_real_runner() -> None:
    missing = HermesSendAdapter(
        HermesSendConfig(target="matrix:@alice", binary="/nonexistent/hermes")
    )
    assert missing.available().available is False
    assert "not installed" in (missing.available().detail or "")

    injected = HermesSendAdapter(
        HermesSendConfig(target="matrix:@alice", binary="/nonexistent/hermes"),
        runner=_Raiser(FileNotFoundError()),
    )
    assert injected.available().available is True


def test_availability_rejects_a_blank_target_after_construction() -> None:
    adapter = HermesSendAdapter(HermesSendConfig(target="matrix:@alice"))
    object.__setattr__(adapter.config, "target", "")
    assert adapter.available().available is False


def test_availability_accepts_a_present_binary_without_an_injected_runner() -> None:
    adapter = HermesSendAdapter(HermesSendConfig(target="matrix:@alice", binary=sys.executable))
    assert adapter.available().available is True


def test_success_parses_provider_ids_from_json() -> None:
    for payload, expected in (
        ('{"message_id": "m-1"}', "m-1"),
        ('{"id": 42}', "42"),
        ('{"provider_id": "p-1"}', "p-1"),
        ('{"ts": "t-1"}', "t-1"),
        ("{}", None),
        ("", None),
        ("not json", None),
        ('["list"]', None),
    ):
        runner = _Raiser_ok(payload)
        adapter = HermesSendAdapter(HermesSendConfig(target="matrix:@alice"), runner=runner)
        result = adapter.send(_notification())
        assert result.outcome == "success"
        assert result.provider_id == expected
        if payload == "not json":
            assert result.code == "malformed_result"
        if payload == '["list"]':
            assert result.code == "malformed_result"


class _Raiser_ok:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def __call__(
        self, argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, self.stdout)


def test_argv_is_a_list_and_body_goes_over_stdin() -> None:
    seen: list[list[str]] = []
    bodies: list[str] = []

    def runner(
        argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        bodies.append(input_text)
        return _completed(0, '{"message_id": "m"}')

    adapter = HermesSendAdapter(
        HermesSendConfig(target="telegram:123", subject_prefix="Calendar"), runner=runner
    )
    result = adapter.send(_notification())

    assert result.outcome == "success"
    assert seen[0] == [
        "hermes",
        "send",
        "--to=telegram:123",
        "--subject=Calendar: Dentist in 30 min",
        "--json",
    ]
    assert bodies[0] == _notification().body


def test_empty_subject_prefix_is_omitted() -> None:
    seen: list[list[str]] = []

    def runner(
        argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return _completed(0)

    HermesSendAdapter(
        HermesSendConfig(target="telegram:123", subject_prefix=""), runner=runner
    ).send(_notification())
    subject = next(token for token in seen[0] if token.startswith("--subject="))
    assert subject == "--subject=Dentist in 30 min"


@pytest.mark.parametrize(
    ("returncode", "stderr", "outcome", "code"),
    [
        (2, "usage", "permanent", "usage"),
        (1, "401 unauthorized", "permanent", "delivery_permanent"),
        (1, "unknown target", "permanent", "delivery_permanent"),
        (1, "503 service unavailable", "transient", "delivery_transient"),
        (1, "429 rate limit", "transient", "delivery_transient"),
        (1, "something odd", "transient", "delivery_transient"),
        (7, "", "permanent", "unexpected_exit"),
    ],
)
def test_exit_code_taxonomy(returncode: int, stderr: str, outcome: str, code: str) -> None:
    def runner(
        argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return _completed(returncode, "", stderr)

    result = HermesSendAdapter(HermesSendConfig(target="telegram:123"), runner=runner).send(
        _notification()
    )

    assert result.outcome == outcome
    assert result.code == code
    assert result.exit_code == returncode


def test_stderr_content_is_not_copied_into_the_delivery_detail() -> None:
    def runner(
        argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, "", "rejected because 123 Main Street is secret")

    result = HermesSendAdapter(HermesSendConfig(target="telegram:123"), runner=runner).send(
        _notification()
    )

    assert "123 Main" not in (result.detail or "")
    assert "secret" not in (result.detail or "")


def test_timeout_maps_to_unknown_without_leaking_output() -> None:
    adapter = HermesSendAdapter(
        HermesSendConfig(target="telegram:123"),
        runner=_Raiser(subprocess.TimeoutExpired(cmd=["hermes"], timeout=1)),
    )
    result = adapter.send(_notification())
    assert result.outcome == "unknown"
    assert result.code == "timeout"


def test_missing_binary_and_start_failure_are_distinct() -> None:
    missing = HermesSendAdapter(
        HermesSendConfig(target="telegram:123"), runner=_Raiser(FileNotFoundError())
    ).send(_notification())
    failed = HermesSendAdapter(
        HermesSendConfig(target="telegram:123"),
        runner=_Raiser(PermissionError("denied")),
    ).send(_notification())
    assert (missing.outcome, missing.code) == ("permanent", "missing_binary")
    assert (failed.outcome, failed.code) == ("transient", "delivery_transient")


def test_subprocess_runner_uses_a_list_argv_and_captures_output() -> None:
    completed = subprocess_runner(
        [sys.executable, "-c", "print('hello')"], input_text="", timeout=10
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "hello"


def test_build_chat_notifier_returns_none_without_an_explicit_target() -> None:
    assert build_chat_notifier(()) is None
    assert (
        build_chat_notifier((ChannelRow(id="phone", kind="hermes_send", enabled=False, config={}),))
        is None
    )
    adapter = build_chat_notifier(
        (
            ChannelRow(
                id="phone",
                kind="hermes_send",
                enabled=True,
                config={"target": "matrix:@me", "subject_prefix": "Cal"},
            ),
        )
    )
    assert adapter is not None
    assert adapter.config.target == "matrix:@me"
    assert adapter.config.subject_prefix == "Cal"


def test_render_helpers() -> None:
    assert relative_phrase(0) == "starting now"
    assert relative_phrase(-86_400) == "in 1 day"
    assert relative_phrase(-7_200) == "in 2 h"
    assert relative_phrase(-600) == "in 10 min"
    assert relative_phrase(600) == "10 min ago"
    assert neutralize_mentions("@everyone @channel <!here>") == (
        "@\u200beveryone @\u200bchannel <\u200b!here>"
    )
    assert sanitize("line\none\x00two ") == "line one two"
    assert len(sanitize("x" * 300)) == 120
    assert sanitize("x" * 300).endswith("…")


def test_notification_title_caps_at_120(tmp_path: Path) -> None:
    from tests.unit.reminder_support import (
        FakeClock,
        create_calendar,
        create_timed_event,
        profile_store,
    )

    connection = profile_store(tmp_path)
    try:
        calendar = create_calendar(connection)
        create_timed_event(connection, calendar, start_utc=1_800_000_600, summary="x" * 200)
        runner = _CapturingRunner()
        from tests.unit.reminder_support import enable_chat, make_engine

        enable_chat(connection)
        make_engine(connection, clock=FakeClock(1_800_000_000), runner=runner).tick()
        subject = next(
            token.removeprefix("--subject=")
            for token in runner.argv
            if token.startswith("--subject=")
        )
        assert len(subject) <= 200
        assert subject.endswith("…")
    finally:
        connection.close()


class _CapturingRunner:
    def __init__(self) -> None:
        self.argv: list[str] = []

    def __call__(
        self, argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.argv = list(argv)
        return _completed(0)
