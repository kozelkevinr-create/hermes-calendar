"""Guard rail: the plugin package must only import itself relatively.

Hermes loads a user plugin directory as a package without putting it on
``sys.path``, so ``from hermes_calendar...`` fails the capability probe. The
only allowed absolute import is the top-level package used by tests, and that
lives outside ``hermes_calendar/`` (the repository root entry point).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "hermes_calendar"
ABSOLUTE_INTERNAL_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+hermes_calendar(?:[.\s]|$)|import[ \t]+hermes_calendar(?:[.\s]|$))",
    re.MULTILINE,
)


def test_package_never_imports_itself_absolutely() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in ABSOLUTE_INTERNAL_IMPORT.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            relative = path.relative_to(REPO_ROOT)
            offenders.append(f"{relative}:{line}: {match.group(0).strip()}")

    assert not offenders, (
        "hermes_calendar/ must use relative imports so Hermes can load the plugin "
        "directory as a package:\n" + "\n".join(offenders)
    )
