"""Plugin registration smoke test.

Hermes is not installed in the test environment, so the context is a stand-in
that records registrations. The scaffold registers nothing; this test pins the
calling convention so a broken import fails CI instead of the plugin loader.
"""

from __future__ import annotations

from hermes_calendar.plugin import register


def test_register_accepts_plugin_context() -> None:
    register(object())
