"""Tables from any source (docx, xlsx, csv, markdown) get identical treatment.

Rows are serialized as ``Header: value; Header: value`` so every chunk is
self-describing (good for embeddings) and exact values stay intact (good for
BM25). Small tables are one chunk; large ones become row groups that repeat the
table label and column list. Large tables also get a TABLE_SUMMARY chunk
(columns, row count, sample rows, categorical values, numeric ranges) so
questions about the table as a whole have something to match.
"""

from __future__ import annotations

from forge_task_documents._util import column_letter
from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft
from forge_task_documents.models import ChunkKind, Element, SourceLocation, TableData


def serialize_row(headers: list[str], row: list[str]) -> str:
    return "; ".join(f"{h}: {v}" for h, v in zip(headers, row, strict=False) if v)


def _as_number(value: str) -> float | None:
    try:
        return float(value.replace(",", "").replace("$", "").replace("%", ""))
    except ValueError:
        return None


class TableStrategy:
    block_kind = BlockKind.TABLE
    version = "1"

    def split(self, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        for el in block.elements:
            if el.table is not None:
                drafts.extend(self._split_table(el, el.table, block, ctx))
        return drafts

    def _split_table(self, el: Element, table: TableData, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        cfg, tok = ctx.config, ctx.tokenizer
        label = table.caption or (block.section_path[-1] if block.section_path else ctx.doc_title)
        header = f"Table: {label}\nColumns: {', '.join(table.headers)}"
        header_tokens = tok.count(header)
        rows = [(i, serialize_row(table.headers, r)) for i, r in enumerate(table.rows, start=1)]
        rows = [(i, text) for i, text in rows if text]
        base_meta = {"columns": table.headers, "row_count": len(table.rows)}

        if not rows:
            return [ChunkDraft(ChunkKind.TABLE, header, list(block.section_path), el.location, base_meta)]

        total = header_tokens + sum(tok.count(t) + 1 for _, t in rows)
        if total <= cfg.max_tokens and len(rows) <= cfg.table_max_rows_per_chunk:
            text = header + "\n" + "\n".join(t for _, t in rows)
            return [ChunkDraft(ChunkKind.TABLE, text, list(block.section_path), el.location, base_meta)]

        drafts: list[ChunkDraft] = []
        if len(table.rows) >= cfg.table_summary_min_rows:
            drafts.append(self._summary(header, table, block, el, ctx))

        budget = cfg.max_tokens - header_tokens - 8  # room for the "Rows a-b of n" line
        group: list[tuple[int, str]] = []
        used = 0

        def flush() -> None:
            if not group:
                return
            first, last = group[0][0], group[-1][0]
            text = f"{header}\nRows {first}-{last} of {len(table.rows)}\n" + "\n".join(t for _, t in group)
            drafts.append(
                ChunkDraft(
                    ChunkKind.TABLE,
                    text,
                    list(block.section_path),
                    self._row_location(el.location, table, first, last),
                    {**base_meta, "row_start": first, "row_end": last},
                )
            )

        for idx, text in rows:
            n = tok.count(text)
            if n > budget:  # one enormous row: truncate to the budget
                text = tok.split(text, max(budget, 16))[0]
                n = tok.count(text)
            if group and (used + n + 1 > budget or len(group) >= cfg.table_max_rows_per_chunk):
                flush()
                group, used = [], 0
            group.append((idx, text))
            used += n + 1
        flush()
        return drafts

    def _summary(self, header: str, table: TableData, block: Block, el: Element, ctx: ChunkContext) -> ChunkDraft:
        cfg = ctx.config
        lines = [header, f"{len(table.rows)} rows."]
        for col, name in enumerate(table.headers):
            values = [r[col] for r in table.rows if col < len(r) and r[col]]
            if not values:
                continue
            numbers = [n for n in (_as_number(v) for v in values) if n is not None]
            distinct = list(dict.fromkeys(values))
            if len(numbers) >= 0.9 * len(values) and len(distinct) > cfg.table_distinct_values_max:
                lines.append(f"{name} ranges from {min(numbers):g} to {max(numbers):g}.")
            elif 1 < len(distinct) <= cfg.table_distinct_values_max and len(values) >= 2 * len(distinct):
                lines.append(f"{name} values: {', '.join(distinct)}.")
        if cfg.table_summary_sample_rows:
            lines.append("Sample rows:")
            for r in table.rows[: cfg.table_summary_sample_rows]:
                lines.append(serialize_row(table.headers, r))
        text = "\n".join(lines)
        if ctx.tokenizer.count(text) > cfg.max_tokens:
            text = ctx.tokenizer.split(text, cfg.max_tokens)[0]
        return ChunkDraft(
            ChunkKind.TABLE_SUMMARY,
            text,
            list(block.section_path),
            el.location,
            {"columns": table.headers, "row_count": len(table.rows)},
        )

    @staticmethod
    def _row_location(loc: SourceLocation, table: TableData, first: int, last: int) -> SourceLocation:
        if table.origin_row is None or table.origin_col is None:
            return loc
        c0 = column_letter(table.origin_col)
        c1 = column_letter(table.origin_col + len(table.headers) - 1)
        rng = f"{c0}{table.origin_row + first}:{c1}{table.origin_row + last}"
        return loc.model_copy(update={"cell_range": rng})
