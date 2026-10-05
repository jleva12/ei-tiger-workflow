"""Diagrams (Visio pages, and anything else that parses to GraphData).

Edges are serialized in flow order (breadth-first from entry nodes) as
``[A] --label--> [B]`` lines, grouped by lane/container when present, and
packed under the token budget. Each chunk lists the Shape Data of the nodes it
mentions. Large diagrams also get a GRAPH_SUMMARY chunk listing every shape.
"""

from __future__ import annotations

from collections import defaultdict, deque

from forge_task_documents.chunking.types import Block, BlockKind, ChunkContext, ChunkDraft
from forge_task_documents.models import ChunkKind, Element, GraphData, GraphEdge, GraphNode


def _edge_line(edge: GraphEdge, nodes: dict[str, GraphNode]) -> str:
    src, dst = nodes[edge.source].label, nodes[edge.target].label
    if edge.label:
        arrow = f"--{edge.label}-->" if edge.directed else f"--{edge.label}--"
    else:
        arrow = "-->" if edge.directed else "---"
    return f"[{src}] {arrow} [{dst}]"


def _flow_order(graph: GraphData) -> list[GraphEdge]:
    out_edges: dict[str, list[GraphEdge]] = defaultdict(list)
    indegree: dict[str, int] = {n.id: 0 for n in graph.nodes}
    for e in graph.edges:
        out_edges[e.source].append(e)
        indegree[e.target] = indegree.get(e.target, 0) + 1
    order: list[GraphEdge] = []
    seen_edges: set[int] = set()
    visited: set[str] = set()
    starts = [n.id for n in graph.nodes if indegree.get(n.id, 0) == 0] + [n.id for n in graph.nodes]
    for start in starts:
        if start in visited:
            continue
        queue = deque([start])
        visited.add(start)
        while queue:
            node = queue.popleft()
            for e in out_edges.get(node, []):
                if id(e) not in seen_edges:
                    seen_edges.add(id(e))
                    order.append(e)
                if e.target not in visited:
                    visited.add(e.target)
                    queue.append(e.target)
    return order


def _node_detail(node: GraphNode) -> str | None:
    if not node.properties:
        return None
    props = "; ".join(f"{k}: {v}" for k, v in node.properties.items())
    return f"{node.label} ({props})"


class GraphStrategy:
    block_kind = BlockKind.GRAPH
    version = "1"

    def split(self, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        for el in block.elements:
            if el.graph is not None and el.graph.nodes:
                drafts.extend(self._split_graph(el, el.graph, block, ctx))
        return drafts

    def _split_graph(self, el: Element, graph: GraphData, block: Block, ctx: ChunkContext) -> list[ChunkDraft]:
        cfg, tok = ctx.config, ctx.tokenizer
        nodes = {n.id: n for n in graph.nodes}
        header = f"Diagram: {graph.name}"
        edges = _flow_order(graph)
        connected = {e.source for e in edges} | {e.target for e in edges}
        isolated = [n for n in graph.nodes if n.id not in connected]

        # (lane, line, node ids involved)
        lines: list[tuple[str | None, str, tuple[str, ...]]] = []
        for e in edges:
            lane = nodes[e.source].group
            lines.append((lane, _edge_line(e, nodes), (e.source, e.target)))
        for n in isolated:
            lines.append((n.group, f"[{n.label}]", (n.id,)))
        if any(lane for lane, _, _ in lines):
            lines.sort(key=lambda item: (item[0] is None, item[0] or ""))  # stable: keeps flow order within a lane

        def render(chunk_lines: list[tuple[str | None, str, tuple[str, ...]]]) -> str:
            out = [header]
            current_lane: str | None = None
            involved: dict[str, None] = {}
            for lane, line, ids in chunk_lines:
                if lane and lane != current_lane:
                    out.append(f"Lane: {lane}")
                    current_lane = lane
                out.append(line)
                involved.update(dict.fromkeys(ids))
            details = [d for d in (_node_detail(nodes[i]) for i in involved) if d]
            if details:
                out.append("Shape data:")
                out.extend(details)
            return "\n".join(out)

        full = render(lines)
        meta = {"node_count": len(graph.nodes), "edge_count": len(graph.edges)}
        if tok.count(full) <= cfg.max_tokens:
            return [ChunkDraft(ChunkKind.GRAPH, full, list(block.section_path), el.location, meta)]

        drafts = [self._summary(header, graph, block, el, ctx, meta)]
        group: list[tuple[str | None, str, tuple[str, ...]]] = []
        for item in lines:
            candidate = group + [item]
            if group and tok.count(render(candidate)) > cfg.max_tokens:
                drafts.append(ChunkDraft(ChunkKind.GRAPH, render(group), list(block.section_path), el.location, meta))
                group = [item]
            else:
                group = candidate
        if group:
            text = render(group)
            if tok.count(text) > cfg.max_tokens:
                text = tok.split(text, cfg.max_tokens)[0]
            drafts.append(ChunkDraft(ChunkKind.GRAPH, text, list(block.section_path), el.location, meta))
        return drafts

    @staticmethod
    def _summary(
        header: str, graph: GraphData, block: Block, el: Element, ctx: ChunkContext, meta: dict[str, int]
    ) -> ChunkDraft:
        lanes = sorted({n.group for n in graph.nodes if n.group})
        lines = [header, f"{len(graph.nodes)} shapes, {len(graph.edges)} connectors."]
        if lanes:
            lines.append("Lanes: " + ", ".join(lanes))
        lines.append("Shapes: " + "; ".join(n.label for n in graph.nodes))
        text = "\n".join(lines)
        if ctx.tokenizer.count(text) > ctx.config.max_tokens:
            text = ctx.tokenizer.split(text, ctx.config.max_tokens)[0]
        return ChunkDraft(ChunkKind.GRAPH_SUMMARY, text, list(block.section_path), el.location, dict(meta))
