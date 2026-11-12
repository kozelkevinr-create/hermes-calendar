"""Delivery adapters for the MVP: ``in_app`` and ``hermes_send``.

The ``hermes send`` adapter is deliberately dumb and isolated. It accepts an
injectable runner so unit tests never invoke Hermes, always uses a list argv (no
shell string, no ``shell=True``), and parses ``--json`` stdout defensively.
Error taxonomy (``app-notifications.md`` §6.6 and appendix A):

===================  ==================  =========  ==================================
condition            outcome             retry?     ``DeliveryResult.code``
===================  ==================  =========  ==================================
exit 0               success             no         ``None``
exit 2               permanent           no         ``usage``
exit 1, 4xx/backend  permanent           no         ``delivery_permanent``
exit 1, 5xx/network  transient           yes        ``delivery_transient``
timeout              unknown             one retry  ``timeout``
executable missing   permanent           no         ``missing_binary``
other exit           permanent           no         ``unexpected_exit``
===================  ==================  =========  ==================================
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..db.reminders import ChannelRow
from .models import Availability, DeliveryResult, Notification

_PERMANENT_HINTS = (
    "400",
    "401",
    "403",
    "404",
    "409",
    "422",
    "invalid",
    "unauthorized",
    "forbidden",
    "not found",
    "rejected",
    "unknown target",
)
_TRANSIENT_HINTS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "rate limit",
    "connection",
    "network",
    "temporarily",
    "unavailable",
)


class CommandRunner(Protocol):
    """The injectable subprocess boundary used by tests."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def subprocess_runner(
    argv: Sequence[str], *, input_text: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run an argv list with no shell, capturing untrusted text output."""
    return subprocess.run(
        list(argv),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )


class Notifier(Protocol):
    """Uniform adapter surface for routing, retry, and observability."""

    name: str

    def available(self) -> Availability: ...

    def send(self, notification: Notification) -> DeliveryResult: ...


class InAppNotifier:
    """The always-available in-app record.

    The ``reminder_instances`` row itself is the in-app notification; this
    adapter records the delivery attempt and never fails.
    """

    name = "in_app"

    def available(self) -> Availability:
        return Availability(True)

    def send(self, notification: Notification) -> DeliveryResult:
        return DeliveryResult(outcome="success", latency_ms=0)


@dataclass(frozen=True)
class HermesSendConfig:
    """Explicit chat delivery configuration.

    ``target`` must come from setup/dashboard configuration. This module never
    calls ``hermes send --list`` and never guesses a room or DM.
    """

    target: str
    subject_prefix: str = "Calendar"
    binary: str = "hermes"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise ValueError("hermes_send requires an explicit target")


class HermesSendAdapter:
    """Deliver one chat message through ``hermes send --json``."""

    name = "hermes_send"

    def __init__(
        self,
        config: HermesSendConfig,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = config
        self._runner = runner

    @property
    def config(self) -> HermesSendConfig:
        """The resolved adapter configuration (target is not a secret)."""
        return self._config

    def available(self) -> Availability:
        """Check explicit configuration and, for the real runner, the binary."""
        if not self._config.target.strip():
            return Availability(False, "no explicit hermes_send target configured")
        if self._runner is None:
            binary = self._config.binary
            found = shutil.which(binary) is not None or Path(binary).is_file()
            if not found:
                return Availability(False, f"{binary!r} is not installed or on PATH")
        return Availability(True)

    def send(self, notification: Notification) -> DeliveryResult:
        """Invoke ``hermes send`` once and map its result onto the taxonomy."""
        started = time.monotonic()
        subject = (
            f"{self._config.subject_prefix}: {notification.title}"
            if self._config.subject_prefix
            else notification.title
        )[:200]
        argv = [
            self._config.binary,
            "send",
            "--to",
            self._config.target,
            "--subject",
            subject,
            "--json",
        ]

        try:
            completed = (
                self._runner(
                    argv,
                    input_text=notification.body,
                    timeout=self._config.timeout_seconds,
                )
                if self._runner is not None
                else subprocess_runner(
                    argv,
                    input_text=notification.body,
                    timeout=self._config.timeout_seconds,
                )
            )
        except subprocess.TimeoutExpired:
            return DeliveryResult(
                outcome="unknown",
                detail="hermes send timed out after the configured limit",
                latency_ms=_latency_ms(started),
                code="timeout",
            )
        except FileNotFoundError:
            return DeliveryResult(
                outcome="permanent",
                detail=f"hermes executable {self._config.binary!r} was not found",
                latency_ms=_latency_ms(started),
                code="missing_binary",
            )
        except OSError:
            return DeliveryResult(
                outcome="transient",
                detail="hermes send could not be started",
                latency_ms=_latency_ms(started),
                code="delivery_transient",
            )

        latency = _latency_ms(started)
        exit_code = completed.returncode
        if exit_code == 0:
            provider_id, detail, code = _parse_success(completed.stdout)
            return DeliveryResult(
                outcome="success",
                provider_id=provider_id,
                detail=detail,
                latency_ms=latency,
                code=code,
                exit_code=0,
            )
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        if exit_code == 2:
            return DeliveryResult(
                outcome="permanent",
                detail="hermes send reported a usage/configuration error (exit 2)",
                latency_ms=latency,
                code="usage",
                exit_code=2,
            )
        if exit_code == 1:
            if _contains_any(stderr, _PERMANENT_HINTS):
                outcome, code = "permanent", "delivery_permanent"
            elif _contains_any(stderr, _TRANSIENT_HINTS):
                outcome, code = "transient", "delivery_transient"
            else:
                # Ambiguity policy: prefer one bounded retry over silence.
                outcome, code = "transient", "delivery_transient"
            return DeliveryResult(
                outcome=outcome,  # type: ignore[arg-type]
                detail=f"hermes send delivery failure (exit 1, {code})",
                latency_ms=latency,
                code=code,
                exit_code=1,
            )
        return DeliveryResult(
            outcome="permanent",
            detail=f"hermes send exited with unexpected status {exit_code}",
            latency_ms=latency,
            code="unexpected_exit",
            exit_code=exit_code,
        )


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def _parse_success(stdout: str | None) -> tuple[str | None, str | None, str | None]:
    """Parse ``--json`` output without trusting it.

    A non-JSON success still means the message was handed off; only the provider
    id is unavailable. Malformed stdout is never echoed into the delivery log.
    """
    if not stdout or not stdout.strip():
        return None, None, None
    try:
        payload: Any = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None, "hermes send returned non-JSON output", "malformed_result"
    if not isinstance(payload, dict):
        return None, "hermes send returned a non-object JSON result", "malformed_result"
    for key in ("message_id", "id", "provider_id", "ts"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value), None, None
    return None, None, None


def build_chat_notifier(
    channels: Sequence[ChannelRow],
    *,
    runner: CommandRunner | None = None,
    timeout_seconds: float = 30.0,
) -> HermesSendAdapter | None:
    """Build the chat adapter from explicit channel configuration.

    Returns ``None`` when no enabled ``hermes_send`` channel has an explicit
    target. There is no fallback to ``hermes send --list``: guessing a room would
    leak calendar content to whoever owns it.
    """
    for channel in channels:
        if channel.kind != "hermes_send" or not channel.enabled:
            continue
        target = channel.config.get("target")
        if not isinstance(target, str) or not target.strip():
            continue
        prefix = channel.config.get("subject_prefix")
        return HermesSendAdapter(
            HermesSendConfig(
                target=target.strip(),
                subject_prefix=prefix if isinstance(prefix, str) else "Calendar",
                timeout_seconds=timeout_seconds,
            ),
            runner=runner,
        )
    return None
