"""Markdown -> IR using markdown-it-py's token stream (CommonMark + GFM tables).

Inline markup is rendered to plain text (emphasis stripped, link text kept),
fenced code keeps its language (a ``mermaid`` fence is read as a diagram, as
the Mermaid parser reads a .mmd file), tables keep header/rows, YAML front
matter goes to metadata. A single leading H1 is treated as the document title
rather than a section, so it doesn't prefix every breadcrumb.
"""

from __future__ import annotations

import re
from typing import Any

from forge_task_documents.models import (
    Element,
    ElementKind,
    ParsedDocument,
    SourceFile,
    SourceLocation,
    TableData,
)
from forge_task_documents.parsers.base import SectionTracker, clean_text, title_from_filename
from forge_task_documents.parsers.mermaid import read_diagram
from forge_task_documents.parsers.text import decode_bytes

_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


class MarkdownParser:
    name = "markdown"
    version = "2"
    extensions = frozenset({"md", "markdown", "mdx"})
    media_types = frozenset({"text/markdown", "text/x-markdown"})

    def parse(self, source: SourceFile) -> ParsedDocument:
        from markdown_it import MarkdownIt

        text = decode_bytes(source.data).replace("\r\n", "\n")
        front, body, line_offset = _split_front_matter(text)
        tokens = MarkdownIt("commonmark").enable("table").parse(body)

        h1_count = sum(1 for t in tokens if t.type == "heading_open" and t.tag == "h1")
        first_block = next((t for t in tokens if t.type.endswith("_open") or t.type in ("fence", "code_block")), None)
        h1_is_title = h1_count == 1 and first_block is not None and first_block.tag == "h1"

        title = str(front.get("title") or "").strip()
        tracker = SectionTracker()
        elements: list[Element] = []
        list_depth = 0
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            loc = _loc(tok.map, line_offset)
            if tok.type in ("bullet_list_open", "ordered_list_open"):
                list_depth += 1
            elif tok.type in ("bullet_list_close", "ordered_list_close"):
                list_depth -= 1
            elif tok.type == "heading_open":
                heading = _inline_text(tokens[i + 1])
                level = int(tok.tag[1])
                if level == 1 and h1_is_title:
                    title = title or heading
                else:
                    path = tracker.push(level, heading)
                    elements.append(
                        Element(kind=ElementKind.HEADING, text=heading, level=level, section_path=path, location=loc)
                    )
                i += 3
                continue
            elif tok.type == "paragraph_open":
                para = _inline_text(tokens[i + 1])
                if para:
                    kind = ElementKind.LIST_ITEM if list_depth else ElementKind.PARAGRAPH
                    elements.append(
                        Element(
                            kind=kind,
                            text=para,
                            level=max(list_depth - 1, 0) if list_depth else None,
                            section_path=tracker.path,
                            location=loc,
                        )
                    )
                i += 3
                continue
            elif tok.type in ("fence", "code_block"):
                code = tok.content.rstrip("\n")
                lang = tok.info.split()[0] if tok.info.strip() else None
                if code.strip() and lang and lang.lower() == "mermaid":
                    # A diagram is searched for what it says, as a .mmd file is.
                    diagram = read_diagram(
                        code,
                        section_path=tracker.path,
                        name=(tracker.path[-1] if tracker.path else title) or None,
                        first_line=(loc.line_start or 0) + 1,
                    )
                    elements.extend(diagram.elements)
                elif code.strip():
                    elements.append(
                        Element(
                            kind=ElementKind.CODE, text=code, language=lang, section_path=tracker.path, location=loc
                        )
                    )
            elif tok.type == "table_open":
                table, i = _collect_table(tokens, i)
                if table is not None:
                    elements.append(
                        Element(kind=ElementKind.TABLE, table=table, section_path=tracker.path, location=loc)
                    )
                continue
            elif tok.type == "html_block":
                stripped = clean_text(_TAG.sub(" ", tok.content))
                if stripped:
                    elements.append(
                        Element(kind=ElementKind.PARAGRAPH, text=stripped, section_path=tracker.path, location=loc)
                    )
            i += 1

        metadata = {k: v for k, v in front.items() if k != "title" and _is_scalar_or_list(v)}
        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="markdown",
            title=title or title_from_filename(source.filename),
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
            metadata=metadata,
        )


def _split_front_matter(text: str) -> tuple[dict[str, Any], str, int]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text, 0
    raw = m.group(1)
    data: dict[str, Any] = {}
    try:
        import yaml

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            data = {str(k): v for k, v in loaded.items()}
    except Exception:
        for line in raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                data[k.strip()] = v.strip().strip("'\"")
    consumed = text[: m.end()]
    return data, text[m.end() :], consumed.count("\n")


def _is_scalar_or_list(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)):
        return True
    return isinstance(value, list) and all(isinstance(v, (str, int, float, bool)) for v in value)


def _loc(token_map: list[int] | None, offset: int) -> SourceLocation:
    if not token_map:
        return SourceLocation()
    return SourceLocation(line_start=token_map[0] + 1 + offset, line_end=token_map[1] + offset)


def _inline_text(token: Any) -> str:
    if token.type != "inline":
        return ""
    if not token.children:
        return clean_text(token.content)
    parts: list[str] = []
    for child in token.children:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type == "softbreak":
            parts.append(" ")
        elif child.type == "hardbreak":
            parts.append("\n")
        elif child.type == "image":
            alt = "".join(c.content for c in (child.children or [])) or child.content
            if alt:
                parts.append(alt)
    return clean_text("".join(parts))


def _collect_table(tokens: list[Any], start: int) -> tuple[TableData | None, int]:
    header: list[str] = []
    rows: list[list[str]] = []
    current: list[str] | None = None
    in_head = False
    i = start + 1
    while i < len(tokens) and tokens[i].type != "table_close":
        t = tokens[i]
        if t.type == "thead_open":
            in_head = True
        elif t.type == "thead_close":
            in_head = False
        elif t.type == "tr_open":
            current = []
        elif t.type == "tr_close" and current is not None:
            if in_head and not header:
                header = current
            else:
                rows.append(current)
            current = None
        elif t.type == "inline" and current is not None:
            current.append(_inline_text(t))
        i += 1
    if not header and not rows:
        return None, i + 1
    width = max([len(header)] + [len(r) for r in rows])
    header = [h or f"Column {n + 1}" for n, h in enumerate(header + [""] * (width - len(header)))]
    rows = [r + [""] * (width - len(r)) for r in rows]
    return TableData(headers=header, rows=rows), i + 1
