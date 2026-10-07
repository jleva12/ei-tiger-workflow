"""A node's exact source, as it was ingested."""

from __future__ import annotations

from dataclasses import dataclass

from forge_codegraph_mcp.graph.errors import IntegrityFailure, InvalidRequest, NotFound
from forge_codegraph_mcp.graph.model import content_sha256, jfield, span_line, span_offset
from forge_codegraph_mcp.graph.query.compact import Summary, summarize
from forge_codegraph_mcp.graph.store import GraphStore

MAX_CONTEXT_LINES = 200
MAX_SOURCE_BYTES = 64 << 10


@dataclass(slots=True)
class SourceResult:
    """The source of a node: ``text_*`` cover the text returned, context
    included; the summary keeps the node's own span."""

    summary: Summary = jfield(factory=Summary, inline=True)
    text: str = jfield("")
    text_start_line: int = jfield(0)
    text_end_line: int = jfield(0)
    text_byte_start: int = jfield(0)
    text_byte_end: int = jfield(0)
    # The span was past the size cap and was cut.
    truncated: bool = jfield(False)


def _line_start(data: bytes, pos: int) -> int:
    """The offset of the first byte of the line holding ``pos``."""
    return data.rfind(b"\n", 0, pos) + 1


def _line_end(data: bytes, pos: int) -> int:
    """The offset just past the newline ending the line holding ``pos``, or
    the end of the data."""
    newline = data.find(b"\n", pos)
    return len(data) if newline < 0 else newline + 1


async def node_source(
    store: GraphStore,
    *,
    repository_id: str,
    node_id: str,
    generation: int = 0,
    context_lines: int = 0,
) -> SourceResult:
    """Cuts the node's span from the retained file behind its anchor, whose
    hash the store verified, so the agent sees the bytes the graph was built
    on, with ``context_lines`` whole lines before and after."""
    if not 0 <= context_lines <= MAX_CONTEXT_LINES:
        raise InvalidRequest(f"context lines 0-{MAX_CONTEXT_LINES}")
    version = await store.get_node(repository_id, node_id, generation)
    node = version.node or {}
    sha = content_sha256(node)
    if not sha:
        raise NotFound(f"node {node_id} has no source anchor")
    data = await store.get_source(repository_id, sha)
    start, end = span_offset(node, "start"), span_offset(node, "end")
    if start > end or end > len(data):
        raise IntegrityFailure(
            f"node {node_id} span [{start},{end}) exceeds its {len(data)}-byte source"
        )
    start_line, end_line = span_line(node, "start"), span_line(node, "end")
    if context_lines > 0:
        # Widen to whole lines, then add the lines asked for around them.
        start = _line_start(data, start)
        if end == 0 or data[end - 1 : end] != b"\n":
            end = _line_end(data, end)
        for _ in range(context_lines):
            if start <= 0:
                break
            start = _line_start(data, start - 1)
            start_line -= 1
        for _ in range(context_lines):
            if end >= len(data):
                break
            end = _line_end(data, end)
            end_line += 1
    result = SourceResult(
        summary=summarize(node),
        text_start_line=start_line,
        text_end_line=end_line,
        text_byte_start=start,
        text_byte_end=end,
    )
    if end - start > MAX_SOURCE_BYTES:
        end = start + MAX_SOURCE_BYTES
        while end > start and data[end] & 0xC0 == 0x80:
            end -= 1
        result.truncated = True
        result.text_byte_end = end
    result.text = data[start:end].decode(errors="replace")
    return result
