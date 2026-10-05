"""Code blocks are atomic. Oversized ones split on blank lines, then lines."""

from __future__ import annotations

from forge_task_documents.chunking.strategies.common import pack_pieces
from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft
from forge_task_documents.models import ChunkKind


class CodeStrategy:
    block_kind = BlockKind.CODE
    version = "1"

    def split(self, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        for el in block.elements:
            meta = {"language": el.language} if el.language else {}
            if ctx.tokenizer.count(el.text) <= ctx.config.max_tokens:
                pieces = [el.text]
            else:
                paragraphs = [p for p in el.text.split("\n\n") if p.strip()]
                pieces = []
                for para in pack_pieces(paragraphs, ctx, joiner="\n\n", overlap=False):
                    if ctx.tokenizer.count(para) <= ctx.config.max_tokens:
                        pieces.append(para)
                    else:
                        pieces.extend(pack_pieces(para.split("\n"), ctx, joiner="\n", overlap=False))
            for piece in pieces:
                drafts.append(ChunkDraft(ChunkKind.CODE, piece, list(block.section_path), el.location, dict(meta)))
        return drafts
