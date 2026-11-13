"""Active Hermes profile identity.

Hermes names a profile by where its home directory sits, not by the last
directory component of the plugin-data path:

* ``home == get_default_hermes_root()`` -> ``default`` (the pre-profile home,
  normally ``~/.hermes``, and also a Docker/custom root such as ``/opt/data``);
* ``home == <root>/profiles/<name>`` -> ``<name>``;
* anything else -> ``custom``.

The earlier derivation, ``profile_dir(name).parent.parent.name``, returned
``.hermes`` for the default profile. That is not a valid profile id, so the
shared-store probe rejected it and the value could be written into the database.

Every call resolves the home and the root again; nothing is cached, so a
process that switches profiles (or a test that moves ``HERMES_HOME``) always
gets the current identity. When ``hermes_constants`` is not importable, the
same ``HERMES_HOME`` fallback as :mod:`hermes_calendar.plugin_storage` is used.

Two unrelated custom homes both report ``custom``: Hermes itself has no other
name for such a home, and inventing one from a path would make the id unstable
and potentially unsafe. The shared-store check uses the id as a display key,
not as a unique machine identity.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

__all__ = [
    "PROFILE_ID_PATTERN",
    "ProfileIdentityError",
    "current_profile_id",
    "validate_profile_id",
]

PROFILE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_PROFILE_ID_RE = re.compile(PROFILE_ID_PATTERN)


class ProfileIdentityError(ValueError):
    """The active profile cannot be represented by a valid profile id.

    Raised before a caller can write an invalid id into a store or pass it to
    the shared-store probe. A ``ValueError`` subclass so callers that already
    validate identifiers keep catching one family of errors.
    """


class _HermesConstants(Protocol):
    """The small slice of ``hermes_constants`` this module relies on."""

    def get_hermes_home(self) -> Path: ...

    def get_default_hermes_root(self) -> Path: ...


def _hermes_constants() -> _HermesConstants | None:
    """Return ``hermes_constants`` when importable, else ``None``.

    Imported lazily so the module also works in tests and off-host tooling. The
    module is looked up on every call, but Python caches it in ``sys.modules``;
    no path is cached here.
    """
    try:
        module: ModuleType = importlib.import_module("hermes_constants")
    except ImportError:
        return None
    return cast(_HermesConstants, module)


def _fallback_profile_home() -> Path:
    """Profile home when ``hermes_constants`` is unavailable."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return Path.home() / ".hermes"
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def _fallback_global_root() -> Path:
    """Root owning all profiles when ``hermes_constants`` is unavailable.

    A profile home normally lives at ``<root>/profiles/<name>``; strip the
    ``profiles/<name>`` segment. Any other ``HERMES_HOME`` is already a root.
    """
    home = _fallback_profile_home()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _resolved(path: Path) -> Path:
    """Normalize a home/root for comparison the way Hermes does."""
    return Path(path).expanduser().resolve(strict=False)


def _derive_profile_id(home: Path, root: Path) -> str:
    """Derive the id from the resolved home and root.

    The returned name may still be invalid (for example a directory under
    ``profiles/`` whose name contains a dot); :func:`current_profile_id`
    validates it and raises instead of returning it.
    """
    resolved_home = _resolved(home)
    resolved_root = _resolved(root)
    if resolved_home == resolved_root:
        return "default"
    profiles_root = resolved_root / "profiles"
    if resolved_home.parent == profiles_root and resolved_home.name:
        return resolved_home.name
    return "custom"


def validate_profile_id(value: str) -> str:
    """Return ``value`` when it is a safe profile id, else raise.

    Profile ids are single path-safe components: letters, digits, ``-`` and
    ``_``, starting with a letter or digit, at most 64 characters. ``default``
    and ``custom`` satisfy this by construction; a directory name that does not
    is refused rather than stored.
    """
    if not isinstance(value, str) or _PROFILE_ID_RE.fullmatch(value) is None:
        raise ProfileIdentityError(
            "invalid profile id: expected letters, digits, '-' and '_' "
            f"(max 64 characters, starting with a letter or digit); got {value!r}"
        )
    return value


def current_profile_id(ctx: Any = None) -> str:
    """Return the active Hermes profile id.

    Args:
        ctx: Optional Hermes context. When it carries a string
            ``profile_name`` that validates, that public value is preferred.
            A missing, non-string, or invalid ``profile_name`` falls back to
            deriving the id from the resolved home and root.

    Returns:
        ``"default"``, a named profile, or ``"custom"``.

    Raises:
        ProfileIdentityError: if the derived id is not a valid profile id.
    """
    if ctx is not None:
        candidate = getattr(ctx, "profile_name", None)
        if isinstance(candidate, str) and _PROFILE_ID_RE.fullmatch(candidate):
            return candidate

    constants = _hermes_constants()
    if constants is not None:
        home = constants.get_hermes_home()
        root = constants.get_default_hermes_root()
    else:
        home = _fallback_profile_home()
        root = _fallback_global_root()

    return validate_profile_id(_derive_profile_id(home, root))
