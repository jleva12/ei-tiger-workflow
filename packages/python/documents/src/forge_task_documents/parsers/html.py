"""HTML -> IR with BeautifulSoup over lxml's HTML parser, which repairs
unclosed and misnested tags the way a browser does.

Only the page's content is read: scripts, styles, forms, ``<nav>``, page-level
``<header>``/``<footer>``/``<aside>`` and hidden elements are dropped, and a page
with a ``<main>`` (or a single ``<article>``) is read from there alone.

``h1``-``h6`` become headings, paragraph-like blocks and loose text paragraphs,
``li`` list items at their nesting depth with their marker (``-``, or ``3.`` in
an ``ol``; a later paragraph of the same item is a ``continuation``), ``pre`` code with its ``language-*``
class (a ``mermaid`` block is read as a diagram, as the Mermaid parser reads a
.mmd file) and ``table`` a table with its header rows, spans and caption. A
table holding another table or headings is layout, and its cells are read as
blocks. Inline markup is flattened to text; ``<br>`` is a line break.

A single leading H1 is the title rather than a section, as in Markdown, so it
doesn't prefix every breadcrumb; it's usually ``<title>`` without the site's
name, which is the title otherwise. The author, description, keywords and
language from ``<head>`` go to metadata.

lxml doesn't report line numbers through BeautifulSoup, so elements have no
location.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from forge_task_documents.models import Element, ElementKind, ParsedDocument, SourceFile, TableData
from forge_task_documents.parsers.base import SectionTracker, clean_text, dedupe_headers, title_from_filename
from forge_task_documents.parsers.mermaid import read_diagram

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# Never content.
_SKIP = frozenset(
    {
        "head", "script", "style", "noscript", "template", "svg", "math", "canvas", "iframe", "object", "embed",
        "video", "audio", "map", "form", "button", "input", "select", "textarea", "label", "dialog", "nav",
    }
)  # fmt: skip
# The page's banner, footer and sidebar, when they aren't inside its content.
_CHROME = frozenset({"header", "footer", "aside"})
_SECTIONING = ["article", "section", "main"]
# Blocks whose own text is one paragraph.
_PARAGRAPHS = frozenset({"p", "dt", "dd", "figcaption", "summary", "caption", "address"})
# Blocks that hold other blocks; loose text in them is a paragraph.
_CONTAINERS = frozenset(
    {
        "html", "body", "main", "article", "section", "div", "header", "footer", "aside", "blockquote", "figure",
        "details", "dl", "center", "hgroup", "search", "fieldset", "tbody", "thead", "tfoot", "tr", "td", "th",
    }
)  # fmt: skip
_LISTS = frozenset({"ul", "ol", "menu", "dir"})
_BLOCKS = sorted(_CONTAINERS | _PARAGRAPHS | _LISTS | set(_HEADINGS) | {"li", "pre", "table", "hr"})
# Wider tables are cut here, so a hostile colspan can't blow up the grid.
_MAX_COLUMNS = 256
_LANGUAGE = re.compile(r"^(?:language|lang|highlight-source|brush:?)-?(.+)$")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")
_SPACE = re.compile(r"[ \t\r\n\f\v]+")
_KEYWORDS = re.compile(r"\s*[,;]\s*")


class HtmlParser:
    name = "html"
    version = "2"
    extensions = frozenset({"html", "htm", "xhtml"})
    media_types = frozenset({"text/html", "application/xhtml+xml"})

    def parse(self, source: SourceFile) -> ParsedDocument:
        from bs4 import BeautifulSoup

        # Bytes that read as UTF-8 almost surely are; otherwise BeautifulSoup
        # honours the page's <meta charset> and sniffs the rest.
        try:
            markup: str | bytes = source.data.decode("utf-8-sig")
        except UnicodeDecodeError:
            markup = source.data
        soup = BeautifulSoup(markup, "lxml")

        root = _content_root(soup)
        h1s = [h for h in root.find_all("h1") if _is_read(h, root)]
        reader = _Reader(lone_h1=h1s[0] if len(h1s) == 1 else None)
        reader.walk(root)
        reader.flush()

        head_title = _inline_text(soup.title, line_breaks=False) if soup.title else ""
        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="html",
            title=reader.title or head_title or title_from_filename(source.filename),
            elements=reader.elements,
            parser_name=self.name,
            parser_version=self.version,
            metadata=_metadata(soup),
        )


def _content_root(soup: Any) -> Any:
    main = soup.find("main")
    if main is not None:
        return main
    articles = soup.find_all("article")
    if len(articles) == 1:
        return articles[0]
    return soup.body or soup


def _metadata(soup: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for tag in soup.find_all("meta"):
        name = str(tag.get("name") or "").strip().lower()
        content = clean_text(_SPACE.sub(" ", str(tag.get("content") or "")))
        if not content or name not in ("author", "description", "keywords") or name in meta:
            continue
        meta[name] = [k for k in _KEYWORDS.split(content) if k] if name == "keywords" else content
    html = soup.find("html")
    lang = str(html.get("lang") or "").strip() if html is not None else ""
    if lang:
        meta["language"] = lang
    return meta


def _is_skipped(tag: Any) -> bool:
    return tag.name in _SKIP or _is_hidden(tag) or _is_chrome(tag)


def _is_hidden(tag: Any) -> bool:
    if tag.has_attr("hidden") or str(tag.get("aria-hidden") or "").lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(str(tag.get("style") or "").lower()))


def _is_chrome(tag: Any) -> bool:
    return tag.name in _CHROME and tag.find_parent(_SECTIONING) is None


def _is_read(tag: Any, root: Any) -> bool:
    """Whether walking ``root`` reaches ``tag``: nothing it's in is skipped."""
    node = tag
    while node is not None and node is not root:
        if _is_skipped(node):
            return False
        node = node.parent
    return True


def _classes(tag: Any) -> list[str]:
    return [str(c).lower() for c in tag.get("class") or []]


def _int_attr(tag: Any, name: str) -> int | None:
    try:
        return int(str(tag.get(name)).strip())
    except (TypeError, ValueError):
        return None


def _list_start(ol: Any) -> int:
    start = _int_attr(ol, "start")
    return 1 if start is None else start


def _has_block(tag: Any) -> bool:
    return tag.find(_BLOCKS) is not None


def _is_layout_table(table: Any) -> bool:
    if str(table.get("role") or "").lower() in ("presentation", "none"):
        return True
    return table.find(["table", *_HEADINGS]) is not None


@dataclass
class _Reader:
    """Walks the content root top to bottom, collecting loose inline text
    until the next block closes it into a paragraph (or list item)."""

    lone_h1: Any = None
    title: str = ""
    elements: list[Element] = field(default_factory=list)
    tracker: SectionTracker = field(default_factory=SectionTracker)
    list_depth: int = 0
    # The next number of each open list, None for an unordered one.
    _lists: list[int | None] = field(default_factory=list)
    # One entry per open item: its marker until its first paragraph takes it.
    _items: list[str | None] = field(default_factory=list)
    _pending: list[str] = field(default_factory=list)

    def walk(self, node: Any) -> None:
        from bs4 import Tag

        for child in node.children:
            if not isinstance(child, Tag):
                _inline(child, self._pending)
                continue
            if _is_skipped(child):
                continue
            name = child.name
            if name in _HEADINGS:
                self.flush()
                self._heading(child, _HEADINGS[name])
            elif name == "pre":
                self.flush()
                self._code(child)
            elif name == "table":
                self.flush()
                if _is_layout_table(child):
                    self.walk(child)
                    self.flush()
                else:
                    self._table(child)
            elif name in _LISTS:
                self.flush()
                self.list_depth += 1
                self._lists.append(_list_start(child) if name == "ol" else None)
                self.walk(child)
                self.flush()
                self._lists.pop()
                self.list_depth -= 1
            elif name == "li":
                self.flush()
                depth = self.list_depth
                self.list_depth = max(depth, 1)  # an <li> outside any list is still an item
                self._items.append(self._marker(child))
                self.walk(child)
                self.flush()
                self._items.pop()
                self.list_depth = depth
            elif name == "div" and "mermaid" in _classes(child) and not _has_block(child):
                self.flush()
                self._diagram(child.get_text())
            elif name in _PARAGRAPHS or name in _CONTAINERS or name == "hr" or _has_block(child):
                # An inline element wrapping blocks (an <a> around a card) is a container too.
                self.flush()
                self.walk(child)
                self.flush()
            else:
                _inline(child, self._pending)

    def flush(self) -> None:
        text = clean_text("".join(self._pending))
        self._pending.clear()
        if not text:
            return
        if self.list_depth:
            self.elements.append(
                Element(
                    kind=ElementKind.LIST_ITEM,
                    text=text,
                    level=self.list_depth - 1,
                    section_path=self.tracker.path,
                    attrs=self._item_attrs(),
                )
            )
        else:
            self.elements.append(Element(kind=ElementKind.PARAGRAPH, text=text, section_path=self.tracker.path))

    def _marker(self, li: Any) -> str:
        """``-`` in a bullet list; the item's number in an ``ol``, which
        ``<li value>`` resets as a browser does."""
        if not self._lists or self._lists[-1] is None:
            return "-"
        number = _int_attr(li, "value")
        if number is None:
            number = self._lists[-1]
        self._lists[-1] = number + 1
        return f"{number}."

    def _item_attrs(self) -> dict[str, Any]:
        if not self._items:
            return {}
        marker = self._items[-1]
        if marker is None:
            return {"continuation": True}
        self._items[-1] = None
        return {"marker": marker}

    def _heading(self, tag: Any, level: int) -> None:
        text = _inline_text(tag, line_breaks=False)
        if not text:
            return
        if tag is self.lone_h1 and not self.elements:
            self.title = text
            return
        path = self.tracker.push(level, text)
        self.elements.append(Element(kind=ElementKind.HEADING, text=text, level=level, section_path=path))

    def _code(self, pre: Any) -> None:
        code = pre.find("code")
        language = _language(pre) or (_language(code) if code is not None else None)
        for br in pre.find_all("br"):  # some highlighters break lines with <br>
            br.replace_with("\n")
        text = pre.get_text()
        # A newline right after <pre> isn't content (HTML spec).
        text = (text[1:] if text.startswith("\n") else text).rstrip()
        if not text.strip():
            return
        if language == "mermaid" or "mermaid" in _classes(pre):
            self._diagram(text)
            return
        self.elements.append(
            Element(kind=ElementKind.CODE, text=text, language=language, section_path=self.tracker.path)
        )

    def _diagram(self, text: str) -> None:
        text = text.strip("\n")
        if not text.strip():
            return
        diagram = read_diagram(
            text,
            section_path=self.tracker.path,
            name=(self.tracker.path[-1] if self.tracker.path else self.title) or None,
        )
        self.elements.extend(diagram.elements)

    def _table(self, table: Any) -> None:
        data = _table_data(table)
        if data is not None:
            self.elements.append(Element(kind=ElementKind.TABLE, table=data, section_path=self.tracker.path))


def _language(tag: Any) -> str | None:
    data_lang = str(tag.get("data-lang") or tag.get("data-language") or "").strip().lower()
    if data_lang:
        return data_lang
    for cls in _classes(tag):
        m = _LANGUAGE.match(cls)
        if m:
            return m.group(1)
    return None


def _inline(node: Any, parts: list[str], *, line_breaks: bool = True) -> None:
    """Append the text a reader sees of ``node``: whitespace collapsed (not
    trimmed: it separates words across tags), ``<br>`` a line break, an image
    its alt text, comments, scripts and hidden elements left out."""
    from bs4 import NavigableString, Tag

    if isinstance(node, NavigableString):
        if type(node) is NavigableString:  # not a comment, doctype, CDATA, ...
            parts.append(_SPACE.sub(" ", str(node)))
    elif isinstance(node, Tag) and node.name not in _SKIP and not _is_hidden(node):
        if node.name == "br":
            parts.append("\n" if line_breaks else " ")
        elif node.name == "img":
            alt = str(node.get("alt") or "").strip()
            if alt:
                parts.append(alt)
        else:
            for child in node.children:
                _inline(child, parts, line_breaks=line_breaks)


def _inline_text(tag: Any, *, line_breaks: bool = True) -> str:
    parts: list[str] = []
    for child in tag.children:
        _inline(child, parts, line_breaks=line_breaks)
    return clean_text("".join(parts))


def _span(cell: Any, attr: str, limit: int) -> int:
    try:
        return max(1, min(int(str(cell.get(attr) or 1)), limit))
    except ValueError:
        return 1


def _table_data(table: Any) -> TableData | None:
    caption_tag = table.find("caption")
    caption = _inline_text(caption_tag, line_breaks=False) if caption_tag is not None else ""
    trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]

    # Lay the cells out on a grid, so a spanned cell keeps the columns after
    # it aligned. A body cell's text goes in its first slot, as the Word
    # parser does; a header cell's repeats across its span, since it names
    # every column under it.
    grid: list[list[str | None]] = [[] for _ in trs]
    header_rows = 0
    for r, tr in enumerate(trs):
        cells = tr.find_all(["td", "th"], recursive=False)
        in_head = tr.find_parent("thead") is not None or (bool(cells) and all(c.name == "th" for c in cells))
        if in_head and r == header_rows:
            header_rows += 1
        row = grid[r]
        col = 0
        for cell in cells:
            while col < len(row) and row[col] is not None:
                col += 1
            if col >= _MAX_COLUMNS:
                break
            text = _inline_text(cell, line_breaks=False)
            colspan = _span(cell, "colspan", _MAX_COLUMNS - col)
            rowspan = _span(cell, "rowspan", len(trs) - r)
            for dr in range(rowspan):
                target = grid[r + dr]
                target.extend([None] * (col + colspan - len(target)))
                for dc in range(colspan):
                    target[col + dc] = text if in_head or (dr == 0 and dc == 0) else ""
            col += colspan

    head = [[c or "" for c in row] for row in grid[:header_rows]]
    body = [[c or "" for c in row] for row in grid[header_rows:] if any(row)]
    if not head:
        if not body:
            return None
        head, body = body[:1], body[1:]
    width = max(len(row) for row in head + body)
    headers: list[str] = []
    for n in range(width):
        parts: list[str] = []
        for header_row in head:
            cell = header_row[n] if n < len(header_row) else ""
            if cell and (not parts or parts[-1] != cell):
                parts.append(cell)
        headers.append(" / ".join(parts) or f"Column {n + 1}")
    rows = [row + [""] * (width - len(row)) for row in body]
    return TableData(headers=dedupe_headers(headers), rows=rows, caption=caption or None)
