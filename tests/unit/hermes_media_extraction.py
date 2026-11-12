"""Hermes outbound media extraction, copied for black-box assertions.

Source: ``~/.hermes/hermes-agent/gateway/platforms/base.py`` on ``docker-host``
at the revision used for the 2026-11-12 review:

* ``MEDIA_DELIVERY_EXTS`` (lines 1176-1188);
* ``_MEDIA_CJK_TERMINATORS`` and ``MEDIA_TAG_CLEANUP_RE`` (lines 1213-1227);
* ``MEDIA_EXTENSIONLESS_TAG_RE`` (lines 1248-1253);
* ``_has_media_directives`` and ``extract_media`` (lines 1312 and 3203-3248).

This copy is intentionally test-only. If Hermes changes the extraction, the
plugin test must be updated alongside it; the production neutralizer cites the
same source lines in ``hermes_calendar/reminders/render.py``.
"""

from __future__ import annotations

import re

MEDIA_DELIVERY_EXTS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".svg",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".3gp",
    ".mp3",
    ".m2a",
    ".wav",
    ".ogg",
    ".opus",
    ".m4a",
    ".flac",
    ".pdf",
    ".docx",
    ".doc",
    ".odt",
    ".rtf",
    ".txt",
    ".md",
    ".epub",
    ".xlsx",
    ".xls",
    ".ods",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".kmz",
    ".kml",
    ".geojson",
    ".gpx",
    ".pptx",
    ".ppt",
    ".odp",
    ".key",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".apk",
    ".ipa",
    ".html",
    ".htm",
)

_MEDIA_EXT_ALTERNATION = "|".join(
    sorted((item.lstrip(".") for item in MEDIA_DELIVERY_EXTS), key=len, reverse=True)
)
_MEDIA_CJK_TERMINATORS = "（）〈〉《》：，。；！？、\u201c\u201d\u2018\u2019【】"

MEDIA_TAG_CLEANUP_RE = re.compile(
    r"""[`"'*_]{0,3}MEDIA:\s*"""
    r"""(?P<path>`[^`\n]+?`|"[^"\n]+?"|'[^'\n]+?'|"""
    r"""(?:~/|/|[A-Za-z]:[/\\])\S+?(?:[^\S\n]+\S+?)*?\.(?:""" + _MEDIA_EXT_ALTERNATION + r"""))"""
    r"""(?=[\s`"'*_,;:)\]}\["""
    + _MEDIA_CJK_TERMINATORS
    + r"""]|MEDIA:|\.(?:\s|$)|$)[`"'*_]{0,3}\.?""",
    re.IGNORECASE,
)

MEDIA_EXTENSIONLESS_TAG_RE = re.compile(
    r"""[`"'*_]{0,3}MEDIA:\s*"""
    r"""(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|"""
    r"""(?:~/|/|[A-Za-z]:[/\\])[^\s\n`"']+?)"""
    r"""(?=[`"'\s,;:)\]}""" + _MEDIA_CJK_TERMINATORS + r"""]|MEDIA:|$)"""
    r"""[`"'*_]{0,3}\s*""",
    re.IGNORECASE,
)

BRACKET_MARKERS = ("[[audio_as_voice]]", "[[as_document]]")


def directives_in(text: str) -> list[str]:
    """Return every directive Hermes would recognize in ``text``.

    ``extract_media`` runs both MEDIA regexes and removes the two exact bracket
    markers before returning delivered paths. Any non-empty result here is a
    file-attachment or delivery-modifier directive.
    """
    found = [match.group(0) for match in MEDIA_TAG_CLEANUP_RE.finditer(text)]
    found.extend(match.group(0) for match in MEDIA_EXTENSIONLESS_TAG_RE.finditer(text))
    found.extend(marker for marker in BRACKET_MARKERS if marker in text)
    return found
