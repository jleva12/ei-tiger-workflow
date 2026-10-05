"""Deterministic identifiers.

chunk id      = f(tenant, doc, section, text, occurrence)  -> stable across re-ingests,
                independent of position, so inserting a paragraph doesn't
                rename every chunk after it.
content hash  -> forge_embeddings.embedding.stage.content_hash
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

_SEP = "\x1f"


def _h(*parts: str, size: int = 32) -> str:
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()[:size]


def section_key(tenant_id: str, doc_id: str, section_path: Sequence[str]) -> str:
    return _h(tenant_id, doc_id, *section_path, size=16)


def chunk_id(tenant_id: str, doc_id: str, section: str, text: str, occurrence: int) -> str:
    return _h(tenant_id, doc_id, section, _h(text, size=64), str(occurrence))


def config_fingerprint(*parts: str) -> str:
    return _h(*parts, size=10)
