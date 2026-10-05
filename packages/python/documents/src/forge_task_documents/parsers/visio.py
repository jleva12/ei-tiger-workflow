"""Visio (.vsdx) -> IR as a graph per page, using the ``vsdx`` library.

Diagrams are not prose: what users search for is "what happens after X",
"who approves Y". So each page becomes one GRAPH element with:
  nodes  - text-bearing 2-D shapes (+ Shape Data properties)
  edges  - glued connectors (BeginX -> EndX), labelled with connector text
  group  - enclosing group shape text, or container/swimlane membership
           (Visio stores it as DEPENDSON(4, Sheet.N!...) in the Relationships cell)
Legacy binary .vsd is not supported here; convert it, or add a parser backed
by libvisio-ng.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from typing import Any

from forge_task_documents.errors import ParseError
from forge_task_documents.models import (
    Element,
    ElementKind,
    GraphData,
    GraphEdge,
    GraphNode,
    ParsedDocument,
    SourceFile,
    SourceLocation,
)
from forge_task_documents.parsers.base import clean_text, title_from_filename

_CONTAINER_REF = re.compile(r"DEPENDSON\(\s*4\s*,([^)]*)\)", re.IGNORECASE)
_SHEET_ID = re.compile(r"Sheet\.(\d+)!")


class VisioParser:
    name = "visio"
    version = "1"
    extensions = frozenset({"vsdx", "vsdm"})
    media_types = frozenset(
        {
            "application/vnd.ms-visio.drawing.main+xml",
            "application/vnd.ms-visio.drawing",
            "application/vnd.visio",
        }
    )

    def parse(self, source: SourceFile) -> ParsedDocument:
        try:
            from vsdx import VisioFile
        except ImportError as exc:  # pragma: no cover
            raise ParseError("vsdx is not installed") from exc

        elements: list[Element] = []
        page_names: list[str] = []
        # vsdx only opens real paths ending in .vsdx and extracts next to them.
        with tempfile.TemporaryDirectory(prefix="vsdx-") as tmp:
            path = os.path.join(
                tmp, "diagram." + (source.extension if source.extension in ("vsdx", "vsdm") else "vsdx")
            )
            with open(path, "wb") as fh:
                fh.write(source.data)
            try:
                with VisioFile(path) as vis:
                    for index, page in enumerate(vis.pages, start=1):
                        name = (getattr(page, "name", None) or f"Page {index}").strip()
                        page_names.append(name)
                        graph = _page_graph(page, name)
                        if graph.nodes:
                            elements.append(
                                Element(
                                    kind=ElementKind.GRAPH,
                                    graph=graph,
                                    section_path=[name],
                                    location=SourceLocation(page=index, page_name=name),
                                )
                            )
            except ParseError:
                raise
            except Exception as exc:
                raise ParseError(f"cannot read Visio file {source.filename!r}: {exc}") from exc

        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="visio",
            title=title_from_filename(source.filename),
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
            metadata={"pages": page_names},
        )


def _master_name(shape: Any) -> str | None:
    master = getattr(shape, "master_page", None)
    name = getattr(master, "name", None) if master is not None else None
    return str(name) if name else None


def _data_properties(shape: Any) -> dict[str, str]:
    props: dict[str, str] = {}
    try:
        raw = shape.data_properties or {}
    except Exception:
        return props
    for label, prop in raw.items():
        value = getattr(prop, "value", None)
        value_str = clean_text(str(value)) if value not in (None, "") else ""
        if value_str:
            props[str(getattr(prop, "label", None) or label)] = value_str
    return props


def _container_ids(shape: Any) -> list[str]:
    try:
        formula = shape.cell_formula("Relationships") or ""
    except Exception:
        return []
    ids: list[str] = []
    for group in _CONTAINER_REF.findall(formula):
        ids.extend(_SHEET_ID.findall(group))
    return ids


def _page_graph(page: Any, page_name: str) -> GraphData:
    connects = list(page.connects)
    connector_ids = {c.from_id for c in connects}
    shapes = list(page.all_shapes)
    by_id = {s.ID: s for s in shapes}

    # group/container labels
    parent_label: dict[str, str] = {}
    for s in shapes:
        if s.shape_type == "Group":
            label = clean_text(s.text or "")
            if label:
                for child in s.all_shapes if hasattr(s, "all_shapes") else s.child_shapes:
                    if child.ID != s.ID:
                        parent_label.setdefault(child.ID, label)
    container_of: dict[str, str] = {}
    container_ids: set[str] = set()
    for s in shapes:
        for cid in _container_ids(s):
            container = by_id.get(cid)
            if container is not None:
                label = clean_text(container.text or "")
                container_ids.add(cid)
                if label:
                    container_of[s.ID] = label

    nodes: list[GraphNode] = []
    node_ids: set[str] = set()
    for s in shapes:
        master = _master_name(s)
        is_connector = s.ID in connector_ids or (master is not None and "connector" in master.lower())
        if is_connector or s.ID in container_ids:
            continue
        if s.shape_type == "Group" and s.child_shapes:
            continue
        label = clean_text(s.text or "").replace("\n", " ")
        props = _data_properties(s)
        if not label and not props:
            continue
        nodes.append(
            GraphNode(
                id=s.ID,
                label=label or master or f"Shape {s.ID}",
                shape_type=master,
                group=container_of.get(s.ID) or parent_label.get(s.ID),
                properties=props,
            )
        )
        node_ids.add(s.ID)

    ends: dict[str, dict[str, str]] = defaultdict(dict)
    for c in connects:
        rel = (c.from_rel or "").lower()
        if rel.startswith("begin"):
            ends[c.from_id]["source"] = c.to_id
        elif rel.startswith("end"):
            ends[c.from_id]["target"] = c.to_id

    edges: list[GraphEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for connector_id, e in ends.items():
        src, dst = e.get("source"), e.get("target")
        if not src or not dst or src not in node_ids or dst not in node_ids:
            continue
        connector = by_id.get(connector_id)
        label = clean_text(connector.text or "") if connector is not None else ""
        key = (src, dst, label)
        if key in seen:
            continue
        seen.add(key)
        edges.append(GraphEdge(source=src, target=dst, label=label or None))

    return GraphData(name=page_name, nodes=nodes, edges=edges)
