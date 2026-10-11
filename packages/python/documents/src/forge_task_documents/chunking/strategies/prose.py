"""Headings, paragraphs and list items within one section.

Elements are packed greedily up to ``max_tokens`` without ever splitting an
element that fits. Oversized paragraphs are split on sentence boundaries
(with a one-sentence overlap), and only a single giant sentence falls back to
a hard token-window split.

Lists keep their shape: items are indented by depth under their marker, a
list item together with everything nested under it is kept in one chunk when
it fits (once the chunk has ``min_tokens`` in it, a subtree that doesn't fit
starts the next chunk rather than being cut), and a chunk that starts inside
a list repeats the items above it, so ``- Up to $2,500`` is never read without
the ``- Adjusters`` / ``  - Home claims`` it belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_task_documents.chunking.strategies.common import merge_locations, pack_pieces, split_sentences
from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft
from forge_task_documents.models import ChunkKind, Element, ElementKind, SourceLocation

# A repeated parent item is cut to this many characters.
_TRAIL_LINE_CHARS = 120


@dataclass(slots=True)
class _Unit:
    text: str
    tokens: int
    location: SourceLocation
    is_list: bool
    # List depth of the item this unit belongs to; -1 outside a list.
    depth: int = -1
    # The start of a list item (not a later paragraph or piece of one).
    starts_item: bool = False
    # Rendered lines of the items above it, outermost first, repeated when a
    # chunk starts here.
    trail: tuple[str, ...] = ()
    trail_tokens: int = 0


def _marker(el: Element) -> str:
    return str(el.attrs.get("marker") or "-")


def render_element(el: Element) -> str:
    if el.kind is ElementKind.LIST_ITEM:
        indent = "  " * (el.level or 0)
        if el.attrs.get("continuation"):
            return indent + "  " + el.text
        return indent + _marker(el) + " " + el.text
    return el.text


def _trail_line(rendered: str) -> str:
    first = rendered.split("\n", 1)[0]
    return first if len(first) <= _TRAIL_LINE_CHARS else first[: _TRAIL_LINE_CHARS - 1].rstrip() + "…"


class ProseStrategy:
    block_kind = BlockKind.PROSE
    version = "2"

    def split(self, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        return self._pack(self._units(block, ctx), block, ctx)

    def _units(self, block: Block, ctx: ChunkContext) -> list[_Unit]:
        max_tokens = ctx.config.max_tokens
        units: list[_Unit] = []
        # The rendered line of the open item at each depth (None: skipped depth).
        open_items: list[str | None] = []
        for el in block.elements:
            rendered = render_element(el)
            if not rendered.strip():
                continue
            if el.kind is not ElementKind.LIST_ITEM:
                open_items.clear()
                n = ctx.tokenizer.count(rendered)
                if n <= max_tokens:
                    units.append(_Unit(rendered, n, el.location, False))
                else:
                    for piece in pack_pieces(split_sentences(rendered), ctx):
                        units.append(_Unit(piece, ctx.tokenizer.count(piece), el.location, False))
                continue

            depth = el.level or 0
            continuation = bool(el.attrs.get("continuation"))
            if continuation:
                above = [line for line in open_items[: depth + 1] if line]
            else:
                del open_items[depth:]
                above = [line for line in open_items if line]
                open_items.extend([None] * (depth - len(open_items)))
                open_items.append(_trail_line(rendered))
            trail, trail_tokens = self._trail(above, ctx)

            n = ctx.tokenizer.count(rendered)
            if trail_tokens + n + 1 <= max_tokens:
                units.append(_Unit(rendered, n, el.location, True, depth, not continuation, trail, trail_tokens))
                continue
            # An item too long for a chunk: split its text on sentences and
            # indent every piece under it; a later piece repeats the item's
            # first line, as a nested item would.
            indent = "  " * depth
            first_lead = indent + ("  " if continuation else _marker(el) + " ")
            lead = indent + " " * (len(first_lead) - len(indent))
            own, own_tokens = self._trail([*above, _trail_line(rendered)], ctx)
            budget = max_tokens - max(trail_tokens, own_tokens) - ctx.tokenizer.count(first_lead) - 1
            piece_ctx = ChunkContext(
                tokenizer=ctx.tokenizer,
                config=ctx.config.model_copy(update={"max_tokens": max(budget, 1)}),
                doc_title=ctx.doc_title,
            )
            for i, piece in enumerate(pack_pieces(split_sentences(el.text), piece_ctx)):
                text = (first_lead if i == 0 else lead) + piece
                units.append(
                    _Unit(
                        text,
                        ctx.tokenizer.count(text),
                        el.location,
                        True,
                        depth,
                        i == 0 and not continuation,
                        trail if i == 0 else own,
                        trail_tokens if i == 0 else own_tokens,
                    )
                )
        return units

    @staticmethod
    def _trail(lines: list[str], ctx: ChunkContext) -> tuple[tuple[str, ...], int]:
        """The parent lines within a quarter of the budget. Short lines are
        kept whole and long ones share what's left, cut to the words that
        fit; when even that can't fit them all, the outermost go first."""
        budget = ctx.config.max_tokens // 4
        lines = list(lines)
        while lines:
            costs = [ctx.tokenizer.count(line) + 1 for line in lines]
            allowed = [0] * len(lines)
            remaining = budget
            order = sorted(range(len(lines)), key=costs.__getitem__)
            for left, i in zip(range(len(order), 0, -1), order, strict=True):
                allowed[i] = min(costs[i], remaining // left)
                remaining -= allowed[i]
            kept: list[str] = []
            for line, cost, allow in zip(lines, costs, allowed, strict=True):
                cut = line if cost <= allow else _shorten(line, allow - 1, ctx)
                if cut is None:
                    break
                kept.append(cut)
            else:
                return tuple(kept), sum(ctx.tokenizer.count(line) + 1 for line in kept)
            lines.pop(0)
        return (), 0

    def _pack(self, units: list[_Unit], block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        max_tokens = ctx.config.max_tokens
        drafts: list[ChunkDraft] = []
        current: list[_Unit] = []
        used = 0

        def flush() -> None:
            nonlocal current, used
            if current:
                drafts.append(self._draft(current, block))
            current, used = [], 0

        def add(unit: _Unit) -> None:
            nonlocal used
            if not current:
                used = unit.trail_tokens
            used += unit.tokens + (1 if current else 0)
            current.append(unit)

        i = 0
        while i < len(units):
            end = _subtree_end(units, i)
            span = units[i:end]
            cost = sum(u.tokens + 1 for u in span)
            if current and used + cost <= max_tokens:
                for u in span:
                    add(u)
                i = end
                continue
            fresh = units[i].trail_tokens + cost
            if fresh <= max_tokens and (not current or used >= ctx.config.min_tokens):
                # The whole item fits a chunk of its own: start one rather than cut it.
                flush()
                for u in span:
                    add(u)
                i = end
                continue
            # Too big for any one chunk (or this chunk is still a runt): take
            # the item's own line, and its children are placed in turn. A
            # split item starts a chunk rather than trailing one as a stub.
            unit = units[i]
            if current and (
                used + unit.tokens + 1 > max_tokens or (unit.starts_item and used >= ctx.config.min_tokens)
            ):
                flush()
            add(unit)
            i += 1
        flush()
        return drafts

    @staticmethod
    def _draft(units: list[_Unit], block: Block) -> ChunkDraft:
        trail = units[0].trail
        text = "\n".join(trail)
        for i, u in enumerate(units):
            if text:
                previous_is_list = units[i - 1].is_list if i else True
                text += "\n" if (u.is_list and previous_is_list) else "\n\n"
            text += u.text
        return ChunkDraft(
            kind=ChunkKind.PROSE,
            text=text,
            section_path=list(block.section_path),
            location=merge_locations(u.location for u in units),
        )


def _shorten(line: str, tokens: int, ctx: ChunkContext) -> str | None:
    """The line's indent, marker and leading words within ``tokens``, ending
    in an ellipsis; None when no word past the marker fits."""
    indent = line[: len(line) - len(line.lstrip(" "))]
    words = line.lstrip(" ").split(" ")
    for keep in range(len(words) - 1, 1, -1):
        cut = indent + " ".join(words[:keep]).rstrip(" ,;:.") + "…"
        if ctx.tokenizer.count(cut) <= tokens:
            return cut
    return None


def _subtree_end(units: list[_Unit], start: int) -> int:
    """One past the last unit of the list item starting at ``start``: its
    later paragraphs and pieces and everything nested under it. Any other
    unit is a span of one."""
    head = units[start]
    if not head.starts_item:
        return start + 1
    end = start + 1
    while end < len(units):
        u = units[end]
        if not u.is_list or (u.starts_item and u.depth <= head.depth):
            break
        end += 1
    return end
