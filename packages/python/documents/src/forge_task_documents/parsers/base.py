"""Small helpers shared by parsers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

_WS = re.compile(r"[ \t ]+")


def clean_text(text: str) -> str:
    """Collapse runs of spaces, trim each line, drop empty edge lines."""
    lines = [_WS.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def title_from_filename(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    return re.sub(r"[_\-]+", " ", stem).strip() or filename


def cell_to_str(value: object) -> str:
    """Stable string form for spreadsheet/table cells."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10g}"
    if hasattr(value, "isoformat"):
        iso = value.isoformat()
        return iso[:-9] if iso.endswith("T00:00:00") else iso
    return clean_text(str(value))


def dedupe_headers(headers: list[str]) -> list[str]:
    """Number repeated column headers: Price, Price (2)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for h in headers:
        n = seen.get(h, 0) + 1
        seen[h] = n
        out.append(h if n == 1 else f"{h} ({n})")
    return out


def core_properties_metadata(cp: Any) -> dict[str, Any]:
    """Author, subject, keywords, category and dates from an OOXML package's
    core properties (python-docx and python-pptx share the interface)."""
    meta: dict[str, Any] = {}
    for key in ("author", "subject", "keywords", "category"):
        value = getattr(cp, key, None)
        if value:
            meta[key] = value
    for key in ("created", "modified"):
        value = getattr(cp, key, None)
        if value is not None:
            meta[key] = value.isoformat()
    return meta


class SectionTracker:
    """Maintains the heading breadcrumb while walking a document top to bottom.

    The document title is NOT part of the path (it lives on ``Chunk.title``);
    ``root`` is for formats with a natural outer level such as a sheet or page.
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = [root] if root else []
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, heading: str) -> list[str]:
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, heading))
        return self.path

    @property
    def path(self) -> list[str]:
        return self._root + [h for _, h in self._stack]
