"""Headings, paragraphs and list items within one section.

Elements are packed greedily up to ``max_tokens`` without ever splitting an
element that fits. Oversized paragraphs are split on sentence boundaries
(with a one-sentence overlap), and only a single giant sentence falls back to
a hard token-window split.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_task_documents.chunking.strategies.common import merge_locations, pack_pieces, split_sentences
from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft
from forge_task_documents.models import ChunkKind, Element, ElementKind, SourceLocation


@dataclass(slots=True)
class _Unit:
    text: str
    tokens: int
    location: SourceLocation
    is_list: bool


def render_element(el: Element) -> str:
    if el.kind is ElementKind.LIST_ITEM:
        return "  " * (el.level or 0) + "- " + el.text
    return el.text


class ProseStrategy:
    block_kind = BlockKind.PROSE
    version = "1"

    def split(self, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        max_tokens = ctx.config.max_tokens
        units: list[_Unit] = []
        for el in block.elements:
            rendered = render_element(el)
            if not rendered.strip():
                continue
            n = ctx.tokenizer.count(rendered)
            is_list = el.kind is ElementKind.LIST_ITEM
            if n <= max_tokens:
                units.append(_Unit(rendered, n, el.location, is_list))
                continue
            for piece in pack_pieces(split_sentences(rendered), ctx):
                units.append(_Unit(piece, ctx.tokenizer.count(piece), el.location, is_list))

        drafts: list[ChunkDraft] = []
        current: list[_Unit] = []
        used = 0
        for unit in units:
            if current and used + unit.tokens + 1 > max_tokens:
                drafts.append(self._draft(current, block))
                current, used = [], 0
            current.append(unit)
            used += unit.tokens + (1 if len(current) > 1 else 0)
        if current:
            drafts.append(self._draft(current, block))
        return drafts

    @staticmethod
    def _draft(units: list[_Unit], block: Block) -> ChunkDraft:
        parts: list[str] = []
        for i, u in enumerate(units):
            if i:
                parts.append("\n" if (u.is_list and units[i - 1].is_list) else "\n\n")
            parts.append(u.text)
        return ChunkDraft(
            kind=ChunkKind.PROSE,
            text="".join(parts),
            section_path=list(block.section_path),
            location=merge_locations(u.location for u in units),
        )
