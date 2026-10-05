"""Pulls exact-match tokens (ticket ids, SKUs, error codes, versions, code
identifiers) out of text. The same extractor runs on chunks at ingest time and
on the query at search time; the identifiers field is indexed with a
keyword+lowercase analyzer so ``ABC-123`` is one token instead of ``abc``+``123``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

DEFAULT_PATTERNS: dict[str, str] = {
    # JIRA-123, INC-000042, PO-7781
    "ticket": r"\b[A-Z][A-Z0-9]{1,9}-\d{1,9}\b",
    # AB12-XY34, 4471-A09-X (letters and digits, dash-separated)
    "part_number": r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{2,}(?:-[A-Z0-9]{2,})+\b",
    # SKU12345, E1001, X9Y8Z7
    "alnum_code": r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,}\b",
    "hex": r"\b0x[0-9A-Fa-f]{2,}\b",
    "version": r"\bv?\d+\.\d+\.\d+(?:\.\d+)?\b",
    "snake_case": r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b",
    "camel_case": r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b",
    "dotted_path": r"\b[a-z_]\w*(?:\.[a-z_]\w*){2,}\b",
}


class IdentifierExtractor:
    def __init__(self, patterns: Mapping[str, str] | None = None, *, max_identifiers: int = 64) -> None:
        self.patterns = dict(DEFAULT_PATTERNS if patterns is None else patterns)
        self._compiled = [re.compile(p) for p in self.patterns.values()]
        self.max_identifiers = max_identifiers

    def extract(self, text: str) -> list[str]:
        found: dict[str, str] = {}
        for rx in self._compiled:
            for m in rx.finditer(text):
                value = m.group(0)
                found.setdefault(value.lower(), value)
                if len(found) >= self.max_identifiers:
                    return list(found.values())
        return list(found.values())
