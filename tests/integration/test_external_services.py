"""Integration tests that need real external services.

These are opt-in (``-m integration``) because they touch credentials, network,
or a running Hermes install. Keep them skipped by default so CI stays offline
and deterministic.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="No integration surface implemented yet")
def test_placeholder() -> None:
    """Placeholder so the integration marker is configured and visible."""
