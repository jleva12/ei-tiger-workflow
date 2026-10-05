from __future__ import annotations

import re
from collections.abc import Iterable

from forge_task_documents.chunking.types import ChunkContext
from forge_task_documents.models import SourceLocation

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def merge_locations(locations: Iterable[SourceLocation]) -> SourceLocation:
    locs = [loc for loc in locations if not loc.is_empty()]
    if not locs:
        return SourceLocation()
    first = locs[0]
    starts = [loc.line_start for loc in locs if loc.line_start is not None]
    ends = [loc.line_end for loc in locs if loc.line_end is not None]
    shape_ids = tuple(dict.fromkeys(s for loc in locs for s in loc.shape_ids))
    return SourceLocation(
        page=first.page,
        page_name=first.page_name,
        sheet=first.sheet,
        cell_range=first.cell_range if len(locs) == 1 else None,
        line_start=min(starts) if starts else None,
        line_end=max(ends) if ends else None,
        shape_ids=shape_ids,
    )


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]


def pack_pieces(pieces: list[str], ctx: ChunkContext, *, joiner: str = " ", overlap: bool = True) -> list[str]:
    """Greedy-pack pieces (sentences, lines) under max_tokens.

    A piece larger than the budget is hard-split with the tokenizer. When
    ``overlap`` is on, the last piece of a window is repeated at the start of
    the next one if it fits in ``overlap_tokens``.
    """
    cfg = ctx.config
    tok = ctx.tokenizer
    out: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for piece in pieces:
        n = tok.count(piece)
        if n > cfg.max_tokens:
            if cur:
                out.append(joiner.join(cur))
                cur, cur_tokens = [], 0
            out.extend(tok.split(piece, cfg.max_tokens, cfg.overlap_tokens))
            continue
        if cur and cur_tokens + n + 1 > cfg.max_tokens:
            out.append(joiner.join(cur))
            carry = cur[-1] if overlap else None
            cur, cur_tokens = [], 0
            if carry is not None:
                carry_n = tok.count(carry)
                if carry_n <= cfg.overlap_tokens and carry_n + n + 1 <= cfg.max_tokens:
                    cur, cur_tokens = [carry], carry_n
        cur.append(piece)
        cur_tokens += n + (1 if len(cur) > 1 else 0)
    if cur:
        out.append(joiner.join(cur))
    return out
