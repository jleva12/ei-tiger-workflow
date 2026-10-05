"""ChunkEngine: one chunker for every format.

1. Group IR elements into blocks: contiguous prose in the same section, or a
   single code/table/graph element.
2. Hand each block to the strategy registered for its block kind.
3. Merge undersized prose chunks with adjacent sibling/parent sections.
4. Assign deterministic ids, ordinals, section keys, token counts.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from forge_task_documents.chunking.ids import chunk_id, config_fingerprint, section_key
from forge_task_documents.chunking.strategies.code import CodeStrategy
from forge_task_documents.chunking.strategies.common import merge_locations
from forge_task_documents.chunking.strategies.graph import GraphStrategy
from forge_task_documents.chunking.strategies.prose import ProseStrategy
from forge_task_documents.chunking.strategies.table import TableStrategy
from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft, ChunkingConfig
from forge_task_documents.models import Chunk, ChunkKind, Element, ElementKind, ParsedDocument

if TYPE_CHECKING:
    from forge_embeddings.protocols import Tokenizer
    from forge_task_documents.protocols import ChunkStrategy

DEFAULT_BLOCK_KINDS: dict[ElementKind, BlockKind] = {
    ElementKind.HEADING: BlockKind.PROSE,
    ElementKind.PARAGRAPH: BlockKind.PROSE,
    ElementKind.LIST_ITEM: BlockKind.PROSE,
    ElementKind.CODE: BlockKind.CODE,
    ElementKind.TABLE: BlockKind.TABLE,
    ElementKind.GRAPH: BlockKind.GRAPH,
}


def default_strategies() -> list[ChunkStrategy]:
    return [ProseStrategy(), TableStrategy(), CodeStrategy(), GraphStrategy()]


def _common_prefix(a: list[str], b: list[str]) -> list[str]:
    out: list[str] = []
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        out.append(x)
    return out


class ChunkEngine:
    ENGINE_VERSION = "1"

    def __init__(
        self,
        tokenizer: Tokenizer,
        config: ChunkingConfig | None = None,
        strategies: Iterable[ChunkStrategy] | None = None,
        block_kinds: Mapping[ElementKind, BlockKind] | None = None,
    ) -> None:
        self.tokenizer = tokenizer
        self.config = config or ChunkingConfig()
        self._strategies: dict[BlockKind, ChunkStrategy] = {}
        for s in strategies if strategies is not None else default_strategies():
            self.register_strategy(s)
        self._block_kinds = dict(block_kinds or DEFAULT_BLOCK_KINDS)

    def register_strategy(self, strategy: ChunkStrategy) -> None:
        self._strategies[strategy.block_kind] = strategy

    @property
    def version(self) -> str:
        fingerprint = config_fingerprint(
            self.ENGINE_VERSION,
            self.tokenizer.name,
            json.dumps(self.config.model_dump(), sort_keys=True),
            *sorted(f"{k}:{type(s).__name__}:{s.version}" for k, s in self._strategies.items()),
        )
        return f"{self.ENGINE_VERSION}-{fingerprint}"

    # ------------------------------------------------------------------ public

    def chunk(self, doc: ParsedDocument) -> list[Chunk]:
        ctx = ChunkContext(tokenizer=self.tokenizer, config=self.config, doc_title=doc.title)
        drafts: list[ChunkDraft] = []
        for block in self.blocks(doc.elements):
            strategy = self._strategies.get(block.kind)
            if strategy is None:
                raise LookupError(f"no chunk strategy registered for block kind {block.kind!r}")
            drafts.extend(d for d in strategy.split(block, ctx) if d.text.strip())
        for d in drafts:
            d.token_count = self.tokenizer.count(d.text)
        if self.config.merge_peers:
            drafts = self._merge_peers(drafts)
        return self._finalize(doc, drafts)

    def blocks(self, elements: Iterable[Element]) -> list[Block]:
        blocks: list[Block] = []
        for el in elements:
            kind = self._block_kinds.get(el.kind, BlockKind.PROSE)
            last = blocks[-1] if blocks else None
            if (
                kind is BlockKind.PROSE
                and last is not None
                and last.kind is BlockKind.PROSE
                and last.section_path == el.section_path
            ):
                last.elements.append(el)
            else:
                blocks.append(Block(kind=kind, section_path=list(el.section_path), elements=[el]))
        return blocks

    # ------------------------------------------------------------------ internals

    def _merge_peers(self, drafts: list[ChunkDraft]) -> list[ChunkDraft]:
        cfg = self.config
        out: list[ChunkDraft] = []
        for d in drafts:
            prev = out[-1] if out else None
            if prev is not None and self._can_merge(prev, d):
                text = prev.text + "\n\n" + d.text
                out[-1] = ChunkDraft(
                    kind=ChunkKind.PROSE,
                    text=text,
                    section_path=_common_prefix(prev.section_path, d.section_path),
                    location=merge_locations([prev.location, d.location]),
                    metadata={**prev.metadata, **d.metadata},
                    token_count=self.tokenizer.count(text),
                )
            else:
                out.append(d)
        # a trailing runt merges backwards if it still fits
        if len(out) >= 2 and out[-1].kind is ChunkKind.PROSE and (out[-1].token_count or 0) < cfg.min_tokens:
            if self._can_merge(out[-2], out[-1]):
                last = out.pop()
                prev = out.pop()
                text = prev.text + "\n\n" + last.text
                out.append(
                    ChunkDraft(
                        ChunkKind.PROSE,
                        text,
                        _common_prefix(prev.section_path, last.section_path),
                        merge_locations([prev.location, last.location]),
                        {**prev.metadata, **last.metadata},
                        self.tokenizer.count(text),
                    )
                )
        return out

    def _can_merge(self, a: ChunkDraft, b: ChunkDraft) -> bool:
        if a.kind is not ChunkKind.PROSE or b.kind is not ChunkKind.PROSE:
            return False
        ta, tb = a.token_count or 0, b.token_count or 0
        if ta >= self.config.min_tokens and tb >= self.config.min_tokens:
            return False
        if ta + tb + 2 > self.config.max_tokens:
            return False
        pa, pb = a.section_path, b.section_path
        prefix = len(_common_prefix(pa, pb))
        shorter, longer = sorted((len(pa), len(pb)))
        # direct parent/child (e.g. intro text + first subsection) ...
        if prefix == shorter and longer - shorter <= 1:
            return True
        # ... or siblings under the same non-root parent. Top-level sections
        # are separate topics and never merge with each other.
        return len(pa) == len(pb) >= 2 and prefix == len(pa) - 1

    def _finalize(self, doc: ParsedDocument, drafts: list[ChunkDraft]) -> list[Chunk]:
        seen: Counter[tuple[str, str]] = Counter()
        chunks: list[Chunk] = []
        version = self.version
        for ordinal, d in enumerate(drafts):
            skey = section_key(doc.tenant_id, doc.doc_id, d.section_path)
            occurrence = seen[(skey, d.text)]
            seen[(skey, d.text)] += 1
            chunks.append(
                Chunk(
                    id=chunk_id(doc.tenant_id, doc.doc_id, skey, d.text, occurrence),
                    tenant_id=doc.tenant_id,
                    doc_id=doc.doc_id,
                    source_type=doc.source_type,
                    kind=d.kind,
                    ordinal=ordinal,
                    title=doc.title,
                    section_path=list(d.section_path),
                    section_key=skey,
                    text=d.text,
                    embed_text=d.text,
                    token_count=d.token_count if d.token_count is not None else self.tokenizer.count(d.text),
                    location=d.location,
                    metadata=dict(d.metadata),
                    chunker_version=version,
                )
            )
        return chunks
