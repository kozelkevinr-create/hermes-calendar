"""The one-per-profile ``hermes cron`` safety net, expressed as text.

The plugin does not create or modify cron jobs on the host: jobs outlive a
plugin, and silently scheduling work from a library call is exactly what
``CLAUDE.md`` warns against. The helper prints the command and the script body
for the user (or installer) to review and run.
"""

from __future__ import annotations

JOB_NAME = "calendar reminders"
SCRIPT_NAME = "hermes-calendar-tick.sh"
SCHEDULE = "1m"

CRON_COMMAND = (
    f'hermes cron create --name "{JOB_NAME}" --no-agent '
    f'--script {SCRIPT_NAME} --deliver local --catch-up on "{SCHEDULE}"'
)

TICK_SCRIPT = """#!/bin/sh
# One-per-profile calendar reminder tick. Keep stdout empty: cron captures it
# and a non-empty result can duplicate the adapter's own chat message.
set -eu
exec python3 -m hermes_calendar reminders tick --quiet
"""


def cron_command() -> str:
    """Return the exact ``hermes cron create`` command to run manually."""
    return CRON_COMMAND


def tick_script() -> str:
    """Return the exact script body the cron job should execute."""
    return TICK_SCRIPT
