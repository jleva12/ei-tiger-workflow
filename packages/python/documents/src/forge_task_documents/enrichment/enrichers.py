"""Enrichers run after chunking and before embedding.

Order matters when composing: LLM context first (it writes ``context``),
then the breadcrumb (it folds context into ``embed_text``), identifiers any time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from forge_embeddings.identifiers import IdentifierExtractor
from forge_task_documents.models import Chunk, ChunkKind, ParsedDocument

if TYPE_CHECKING:
    from forge_embeddings.protocols import LLMClient
    from forge_task_documents.protocols import Enricher

log = logging.getLogger(__name__)


class BreadcrumbEnricher:
    """embed_text = Document / Section / Context header + raw text.

    Only the embedding sees the header; ``text`` stays raw so BM25 scores and
    snippets aren't skewed. Title and section are indexed as their own
    boosted fields for BM25 instead.
    """

    def __init__(self, *, include_title: bool = True) -> None:
        self.include_title = include_title

    async def enrich(self, doc: ParsedDocument, chunks: list[Chunk]) -> list[Chunk]:
        for c in chunks:
            header: list[str] = []
            if self.include_title and c.title:
                header.append(f"Document: {c.title}")
            if c.section_path:
                header.append(f"Section: {' > '.join(c.section_path)}")
            if c.context:
                header.append(f"Context: {c.context}")
            c.embed_text = ("\n".join(header) + "\n\n" + c.text) if header else c.text
        return chunks


class IdentifierEnricher:
    def __init__(self, extractor: IdentifierExtractor | None = None) -> None:
        self.extractor = extractor or IdentifierExtractor()

    async def enrich(self, doc: ParsedDocument, chunks: list[Chunk]) -> list[Chunk]:
        for c in chunks:
            c.identifiers = self.extractor.extract(c.text + "\n" + c.section_text)
        return chunks


CONTEXT_SYSTEM = "<document>\n{document}\n</document>"
CONTEXT_USER = (
    "Here is the chunk we want to situate within the whole document\n"
    "<chunk>\n{chunk}\n</chunk>\n"
    "Please give a short succinct context to situate this chunk within the overall "
    "document for the purposes of improving search retrieval of the chunk. "
    "Answer only with the succinct context and nothing else."
)


class LLMContextEnricher:
    """Contextual retrieval: an LLM writes 1-2 sentences situating each chunk.

    The whole document goes in the (cached) system prompt so each per-chunk
    call only pays for the chunk. Failures are logged and skipped; they never
    fail the ingest.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        kinds: Iterable[ChunkKind] = (ChunkKind.PROSE, ChunkKind.TABLE, ChunkKind.CODE, ChunkKind.GRAPH),
        max_document_chars: int = 180_000,
        max_context_tokens: int = 120,
        concurrency: int = 8,
    ) -> None:
        self.llm = llm
        self.kinds = frozenset(kinds)
        self.max_document_chars = max_document_chars
        self.max_context_tokens = max_context_tokens
        self.concurrency = concurrency
        self._slots = asyncio.Semaphore(concurrency)  # LLM calls at once across this process's jobs

    async def enrich(self, doc: ParsedDocument, chunks: list[Chunk]) -> list[Chunk]:
        """
        Processes a list of chunks and enriches them with additional context using an
        asynchronous approach, if applicable. This function selects specific chunks
        based on their kind, performs context generation for them using a language
        model, and modifies the chunks with the generated context.

        :param doc:
            The document containing the plain text used for contextualization,
            provided as an instance of `ParsedDocument`.

        :param chunks:
            A list of chunks to be processed and potentially enriched. Each chunk
            should be an instance of `Chunk`.

        :return:
            A list of chunks, with selected chunks enriched with contextual
            information. The order of chunks in the returned list matches the
            original input.
        """
        targets = [c for c in chunks if c.kind in self.kinds]
        if not targets:
            return chunks
        system = CONTEXT_SYSTEM.format(document=doc.plain_text(self.max_document_chars))
        sem = self._slots

        async def one(chunk: Chunk) -> None:
            async with sem:
                try:
                    reply = await self.llm.complete(
                        system=system,
                        user=CONTEXT_USER.format(chunk=chunk.text),
                        max_tokens=self.max_context_tokens,
                        cache_system=True,
                    )
                    chunk.context = reply.strip() or None
                except Exception:
                    log.warning("context generation failed for chunk %s", chunk.id, exc_info=True)

        await asyncio.gather(*(one(c) for c in targets))
        return chunks


class CompositeEnricher:
    """
    Handles the orchestration of multiple enrichers to process and enhance
    document chunks.

    This class aggregates multiple enrichers and sequentially applies them to
    enrich the given document chunks. Each enricher processes the chunks in
    the order they are provided, and the output of one enricher is passed as
    input to the next.

    :ivar enrichers: A list of enrichers to be applied to the document chunks.
    :type enrichers: list[Enricher]
    """

    def __init__(self, enrichers: Sequence[Enricher]) -> None:
        self.enrichers = list(enrichers)

    async def enrich(self, doc: ParsedDocument, chunks: list[Chunk]) -> list[Chunk]:
        for enricher in self.enrichers:
            chunks = await enricher.enrich(doc, chunks)
        return chunks
