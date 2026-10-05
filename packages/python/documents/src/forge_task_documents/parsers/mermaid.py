"""Mermaid diagrams (.mmd) -> IR, read without running Mermaid.

Diagrams are searched for what they say happens: "what happens after X", "who
calls Y", "what does an order hold". So each kind becomes what the chunker
writes out best:

- flowcharts, state, class and ER diagrams become one GRAPH element: nodes
  with their labels (subgraphs, composite states and namespaces as groups;
  class members and entity attributes as properties) and labelled edges, which
  the graph strategy writes out in flow order;
- sequence diagrams keep their order: who takes part, then each message as a
  list item and each note as a paragraph, under their loop / alt / opt blocks
  as sections;
- the other kinds (gantt, pie, mindmap, timeline, ...) have no edges worth
  modelling and stay one mermaid code block.

A diagram Mermaid would reject still indexes what can be read of it; one that
yields nothing falls back to the code block. Markdown's ```mermaid fences go
through :func:`read_diagram` too.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

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
from forge_task_documents.parsers.text import decode_bytes

_FRONT_MATTER = re.compile(r"\A\s*---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_DIRECTIVE = re.compile(r"%%\{.*?\}%%", re.DOTALL)
# Line by line, so what's removed keeps its line breaks.
_COMMENT = re.compile(r"^[ \t]*%%.*$", re.MULTILINE)
_ACC_TITLE = re.compile(r"^[ \t]*accTitle[ \t]*:[ \t]*(.*)$", re.MULTILINE)
_ACC_DESCR = re.compile(r"^[ \t]*accDescr\s*(?::\s*(.*?)\s*$|\{(.*?)\})", re.MULTILINE | re.DOTALL)
_TITLE = re.compile(r"^title\s*:?\s+(.+)$", re.IGNORECASE)

# The first word of a diagram, lowercased, to the kinds read here.
_KINDS = {
    "graph": "flowchart",
    "flowchart": "flowchart",
    "flowchart-elk": "flowchart",
    "sequencediagram": "sequence",
    "classdiagram": "class",
    "classdiagram-v2": "class",
    "statediagram": "state",
    "statediagram-v2": "state",
    "erdiagram": "er",
}

# -- Labels ------------------------------------------------------------------

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"#(\d+|[A-Za-z]+);")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(\S(?:.*?\S)?)\1")


def _entity(match: re.Match[str]) -> str:
    code = match.group(1)
    if code.isdigit():
        return chr(int(code)) if int(code) < 0x110000 else ""
    return html.unescape(f"&{code};")


def _text(raw: str | None) -> str:
    """A label as people read it: unquoted, without markup, on one line."""
    if not raw:
        return ""
    text = raw.strip()
    for quote in ('"', "`"):
        if len(text) >= 2 and text[0] == text[-1] == quote:
            text = text[1:-1].strip()
    text = _BR.sub(" ", text)
    text = _TAG.sub("", text)
    text = _ENTITY.sub(_entity, text)
    text = _EMPHASIS.sub(r"\2", html.unescape(text))
    return clean_text(text.replace("\n", " "))


# -- Statements --------------------------------------------------------------


@dataclass
class _Line:
    text: str
    number: int  # 1-based, in the file (or the Markdown document)


def _split(line: str) -> list[str]:
    """A line's statements: split at semicolons outside quotes and brackets."""
    parts: list[str] = []
    depth, quoted, start = 0, False, 0
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif char in "[({":
            depth += 1
        elif char in "])}":
            depth = max(depth - 1, 0)
        elif char == ";" and depth == 0:
            parts.append(line[start:i])
            start = i + 1
    parts.append(line[start:])
    return parts


def _statements(body: str, first_line: int) -> list[_Line]:
    lines: list[_Line] = []
    for offset, raw in enumerate(body.split("\n")):
        for part in _split(raw):
            if part.strip():
                lines.append(_Line(part.strip(), first_line + offset))
    return lines


def _blank(match: re.Match[str]) -> str:
    """What's removed, as its line breaks, so line numbers stay true."""
    return "\n" * match.group(0).count("\n")


# -- Graphs ------------------------------------------------------------------


class _Graph:
    """Nodes by ID, in the order they're met, and the edges between them."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        # The open subgraphs, composite states or namespaces, innermost last.
        self.groups: list[str] = []

    def node(self, node_id: str, label: str | None = None, shape: str | None = None) -> GraphNode:
        node = self.nodes.get(node_id)
        if node is None:
            node = GraphNode(
                id=node_id, label=label or node_id, shape_type=shape, group=self.groups[-1] if self.groups else None
            )
            self.nodes[node_id] = node
            return node
        # A later definition names it, as in Mermaid.
        if label:
            node.label = label
        if shape:
            node.shape_type = shape
        if node.group is None and self.groups:
            node.group = self.groups[-1]
        return node

    def edge(self, source: str, target: str, label: str | None = None, *, directed: bool = True) -> None:
        self.node(source)
        self.node(target)
        self.edges.append(GraphEdge(source=source, target=target, label=label or None, directed=directed))

    def add_property(self, node_id: str, key: str, value: str) -> None:
        if not value:
            return
        properties = self.node(node_id).properties
        properties[key] = f"{properties[key]}; {value}" if key in properties else value

    def data(self, name: str) -> GraphData | None:
        if not self.nodes:
            return None
        return GraphData(name=name, nodes=list(self.nodes.values()), edges=self.edges)


# Flowcharts ------------------------------------------------------------------

_ID = re.compile(r"\s*(\w[\w.]*(?:-\w[\w.]*)*)")
_AMPERSAND = re.compile(r"\s*&")
_CSS_CLASS = re.compile(r":::[\w-]+")
_AT_SHAPE = re.compile(r"@\{(?P<body>[^}]*)\}")
_AT_LABEL = re.compile(r"""label\s*:\s*(?:"(?P<quoted>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^,}]+))""")
_AT_KIND = re.compile(r"shape\s*:\s*([\w-]+)")
# Openers, longest first, with their closers and what the shape usually means.
_SHAPES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("(((", (")))",), "double circle"),
    ("([", ("])",), "stadium"),
    ("[[", ("]]",), "subroutine"),
    ("[(", (")]",), "database"),
    ("((", ("))",), "circle"),
    ("{{", ("}}",), "hexagon"),
    ("[/", ("/]", "\\]"), "parallelogram"),
    ("[\\", ("\\]", "/]"), "parallelogram"),
    (">", ("]",), "flag"),
    ("[", ("]",), "box"),
    ("(", (")",), "rounded box"),
    ("{", ("}",), "decision"),
)
# A link with its label between pipes, or none: -->, ---, ==>, -.->, --o, <-->, ~~~ ...
_PLAIN_LINK = re.compile(
    r"\s*(?:\w+@)?(?P<left><|[ox](?=[-=]))?(?P<body>-{2,}|={2,}|-\.+-|~{3,})(?P<right>>|[ox])?"
    r"\s*(?:\|(?P<label>[^|]*)\|)?"
)
# A link with its label inside: -- text -->, == text ==>, -. text .->
_TEXT_LINKS = (
    re.compile(r"\s*(?:\w+@)?(?P<left><|[ox](?=-))?--(?![->.])\s*(?P<label>.+?)\s*(?P<body>-{2,})(?P<right>>|[ox])?"),
    re.compile(r"\s*(?:\w+@)?(?P<left><|[ox](?==))?==(?![=>])\s*(?P<label>.+?)\s*(?P<body>={2,})(?P<right>>|[ox])?"),
    re.compile(r"\s*(?:\w+@)?(?P<left><|[ox](?=-))?-\.(?![.\-])\s*(?P<label>.+?)\s*(?P<body>\.-+)(?P<right>>|[ox])?"),
)
_SUBGRAPH = re.compile(r"^subgraph\b\s*(?P<rest>.*)$")
_SUBGRAPH_TITLED = re.compile(r"^(?P<id>[\w.-]+)\s*\[(?P<title>.*)\]$")
_FLOWCHART_SKIP = frozenset({"direction", "classdef", "class", "style", "linkstyle", "click", "accdescr", "acctitle"})


@dataclass
class _Link:
    end: int
    label: str
    left: bool
    right: bool
    invisible: bool


def _link(statement: str, pos: int) -> _Link | None:
    plain = _PLAIN_LINK.match(statement, pos)
    # "--" and "==" alone open a link with its label inside.
    if plain and not (plain["body"] in ("--", "==") and not plain["right"]):
        return _Link(
            plain.end(),
            _text(plain["label"]),
            bool(plain["left"]),
            bool(plain["right"]),
            plain["body"].startswith("~"),
        )
    for pattern in _TEXT_LINKS:
        found = pattern.match(statement, pos)
        if found:
            return _Link(found.end(), _text(found["label"]), bool(found["left"]), bool(found["right"]), False)
    return None


def _shape(statement: str, pos: int) -> tuple[int, str | None, str | None] | None:
    """The shape and label right after a node's ID; None when one opens and never closes."""
    for opener, closers, shape in _SHAPES:
        if not statement.startswith(opener, pos):
            continue
        start = pos + len(opener)
        if statement.startswith('"', start):
            quote = statement.find('"', start + 1)
            for closer in closers:
                if quote != -1 and statement.startswith(closer, quote + 1):
                    return quote + 1 + len(closer), statement[start + 1 : quote], shape
        ends = [(found, closer) for closer in closers if (found := statement.find(closer, start)) != -1]
        if not ends:
            return None
        found, closer = min(ends)
        return found + len(closer), statement[start:found], shape
    return pos, None, None


class _Flowchart:
    def __init__(self) -> None:
        self.graph = _Graph()
        self.subgraphs: dict[str, str] = {}

    def read(self, lines: Sequence[_Line]) -> None:
        for line in lines:
            statement = line.text
            word = statement.split(maxsplit=1)[0].lower()
            if word in _FLOWCHART_SKIP:
                continue
            if word == "end":
                if self.graph.groups:
                    self.graph.groups.pop()
                continue
            opened = _SUBGRAPH.match(statement)
            if opened:
                self._open(opened["rest"].strip())
                continue
            self._chain(statement)
        # A subgraph an edge points at is a node, named by its title.
        for node_id, title in self.subgraphs.items():
            node = self.graph.nodes.get(node_id)
            if node is not None and node.label == node_id:
                node.label, node.shape_type = title, "subgraph"

    def _open(self, rest: str) -> None:
        titled = _SUBGRAPH_TITLED.match(rest)
        if titled:
            node_id, title = titled["id"], _text(titled["title"]) or titled["id"]
        else:
            title = _text(rest) or f"subgraph {len(self.subgraphs) + 1}"
            node_id = rest
        self.subgraphs[node_id] = title
        self.graph.groups.append(title)

    def _chain(self, statement: str) -> None:
        first = self._group(statement, 0)
        if first is None:
            return
        left, pos = first
        while (link := _link(statement, pos)) is not None:
            following = self._group(statement, link.end)
            if following is None:
                return
            right, pos = following
            for source in left:
                for target in right:
                    self._connect(source, target, link)
            left = right

    def _connect(self, source: str, target: str, link: _Link) -> None:
        if link.invisible:  # ~~~ only lays nodes out
            return
        if link.left and link.right:
            self.graph.edge(source, target, link.label)
            self.graph.edge(target, source, link.label)
        elif link.left:
            self.graph.edge(target, source, link.label)
        else:
            self.graph.edge(source, target, link.label, directed=link.right)

    def _group(self, statement: str, pos: int) -> tuple[list[str], int] | None:
        """Nodes joined with &, e.g. ``a & b``."""
        ids: list[str] = []
        while (node := self._node(statement, pos)) is not None:
            ids.append(node[0])
            pos = node[1]
            more = _AMPERSAND.match(statement, pos)
            if more is None:
                break
            pos = more.end()
        return (ids, pos) if ids else None

    def _node(self, statement: str, pos: int) -> tuple[str, int] | None:
        found = _ID.match(statement, pos)
        if found is None:
            return None
        node_id, pos = found.group(1), found.end()
        label: str | None = None
        shape: str | None = None
        at = _AT_SHAPE.match(statement, pos)
        if at is not None:
            labelled = _AT_LABEL.search(at["body"])
            if labelled:
                label = next(value for value in labelled.groups() if value is not None)
            kind = _AT_KIND.search(at["body"])
            shape = kind.group(1) if kind else None
            pos = at.end()
        else:
            shaped = _shape(statement, pos)
            if shaped is None:
                return None
            pos, label, shape = shaped
        css = _CSS_CLASS.match(statement, pos)
        if css is not None:
            pos = css.end()
        self.graph.node(node_id, _text(label) or None, shape)
        return node_id, pos


# State diagrams ----------------------------------------------------------------

_TRANSITION = re.compile(r"^(?P<a>\S+?)\s*-->\s*(?P<b>[^:]+?)\s*(?::\s*(?P<label>.*))?$")
_STATE_AS = re.compile(r'^state\s+"(?P<label>[^"]*)"\s+as\s+(?P<id>[\w.-]+)\s*(?P<open>\{)?$')
_STATE = re.compile(r"^state\s+(?P<id>[\w.-]+)\s*(?:<<(?P<kind>\w+)>>)?\s*(?P<open>\{)?$")
_DESCRIPTION = re.compile(r"^(?P<id>[\w.-]+)\s*:\s*(?P<label>.+)$")
_STATE_NOTE = re.compile(r"^note\s+(?:left|right)\s+of\s+(?P<id>[\w.-]+)\s*(?::\s*(?P<text>.*))?$", re.IGNORECASE)
_END_NOTE = re.compile(r"^end\s+note$", re.IGNORECASE)
_STATE_SKIP = frozenset({"direction", "classdef", "class", "style", "--"})


class _StateDiagram:
    def __init__(self) -> None:
        self.graph = _Graph()
        self.composites: list[str] = []

    def read(self, lines: Sequence[_Line]) -> None:
        i = 0
        while i < len(lines):
            statement = _CSS_CLASS.sub("", lines[i].text).strip()
            i += 1
            word = statement.split(maxsplit=1)[0].lower() if statement else ""
            if not statement or word in _STATE_SKIP:
                continue
            if statement == "}":
                if self.composites:
                    self.composites.pop()
                    self.graph.groups.pop()
                continue
            note = _STATE_NOTE.match(statement)
            if note:
                text = note["text"]
                if text is None:  # a note over several lines, to "end note"
                    body: list[str] = []
                    while i < len(lines) and not _END_NOTE.match(lines[i].text):
                        body.append(lines[i].text)
                        i += 1
                    i += 1
                    text = " ".join(body)
                self.graph.add_property(note["id"], "note", _text(text))
                continue
            named = _STATE_AS.match(statement) or _STATE.match(statement)
            if named:
                self._state(named)
                continue
            transition = _TRANSITION.match(statement)
            if transition:
                source = self._end(transition["a"], "start")
                target = self._end(transition["b"].strip(), "end")
                self.graph.edge(source, target, _text(transition["label"]))
                continue
            described = _DESCRIPTION.match(statement)
            if described:
                node = self.graph.node(described["id"])
                label = _text(described["label"])
                if node.label == node.id:
                    node.label = label
                else:
                    self.graph.add_property(node.id, "description", label)

    def _state(self, named: re.Match[str]) -> None:
        groups = named.groupdict()
        label = _text(groups.get("label")) or None
        node = self.graph.node(named["id"], label, groups.get("kind"))
        if named["open"]:
            self.composites.append(node.id)
            self.graph.groups.append(node.label)

    def _end(self, state: str, which: str) -> str:
        """``[*]``: where the diagram, or the composite state it's in, starts or ends."""
        if state != "[*]":
            return state
        inside = self.composites[-1] if self.composites else ""
        node_id = f"[*]{which}:{inside}"
        self.graph.node(node_id, which, which)
        return node_id


# Class diagrams ----------------------------------------------------------------

_NAME = r"[\w.`~]+"
_RELATION = re.compile(
    rf'^(?P<a>{_NAME})\s*(?:"(?P<ca>[^"]*)")?\s*'
    r"(?P<left><\||\*|o|<|\(\))?(?P<line>--|\.\.)(?P<right>\|>|\*|o|>|\(\))?\s*"
    rf'(?:"(?P<cb>[^"]*)")?\s*(?P<b>{_NAME})\s*(?::\s*(?P<label>.*))?$'
)
_CLASS = re.compile(rf'^class\s+(?P<id>{_NAME})\s*(?:\["(?P<label>[^"]*)"\])?\s*(?P<open>\{{)?\s*(?P<rest>.*?)\s*}}?$')
_MEMBER = re.compile(rf"^(?P<id>{_NAME})\s*:\s*(?P<member>.+)$")
_ANNOTATION = re.compile(rf"^<<(?P<kind>[^>]+)>>\s*(?P<id>{_NAME})?$")
_NAMESPACE = re.compile(rf"^namespace\s+(?P<id>{_NAME})\s*\{{$")
_CLASS_NOTE = re.compile(rf'^note\s+(?:for\s+(?P<id>{_NAME})\s+)?"(?P<text>.*)"$', re.IGNORECASE)
_CLASS_SKIP = frozenset({"direction", "classdef", "cssclass", "style", "click", "link", "callback"})
# What each end's marker says; the first word for a solid line, the second for a dashed one.
_MARKERS = {
    "<|": ("inherits from", "implements"),
    "|>": ("inherits from", "implements"),
    "*": ("composed of", "composed of"),
    "o": ("aggregates", "aggregates"),
    "<": ("associated with", "depends on"),
    ">": ("associated with", "depends on"),
    "()": ("provides", "provides"),
}


def _class_name(raw: str) -> str:
    """``List~int~`` as ``List<int>``, the way people write generics."""
    name = raw.strip("`")
    return re.sub(r"~([^~]*)~", r"<\1>", name)


class _ClassDiagram:
    def __init__(self) -> None:
        self.graph = _Graph()
        self.notes: list[tuple[str, _Line]] = []

    def read(self, lines: Sequence[_Line]) -> None:
        body_of: str | None = None  # the class whose { ... } is open
        namespaces = 0
        for line in lines:
            statement = _CSS_CLASS.sub("", line.text).strip()
            word = statement.split(maxsplit=1)[0].lower() if statement else ""
            if not statement or word in _CLASS_SKIP:
                continue
            if body_of is not None:
                if statement == "}":
                    body_of = None
                else:
                    self._member(body_of, statement)
                continue
            if statement == "}":
                if namespaces:
                    namespaces -= 1
                    self.graph.groups.pop()
                continue
            namespace = _NAMESPACE.match(statement)
            if namespace:
                namespaces += 1
                self.graph.groups.append(_class_name(namespace["id"]))
                continue
            note = _CLASS_NOTE.match(statement)
            if note:
                if note["id"]:
                    self.graph.add_property(_class_name(note["id"]), "note", _text(note["text"]))
                else:
                    self.notes.append((_text(note["text"]), line))
                continue
            declared = _CLASS.match(statement)
            if declared:
                name = _class_name(declared["id"])
                self.graph.node(name, _text(declared["label"]) or None)
                if declared["open"]:
                    if declared["rest"]:
                        self._member(name, declared["rest"])
                    if not statement.endswith("}"):
                        body_of = name
                continue
            annotation = _ANNOTATION.match(statement)
            if annotation:
                if annotation["id"]:
                    self.graph.add_property(_class_name(annotation["id"]), "stereotype", annotation["kind"].strip())
                continue
            relation = _RELATION.match(statement)
            if relation:
                self._relation(relation)
                continue
            member = _MEMBER.match(statement)
            if member:
                self._member(_class_name(member["id"]), member["member"])

    def _member(self, name: str, member: str) -> None:
        annotation = _ANNOTATION.match(member.strip())
        if annotation:
            self.graph.add_property(name, "stereotype", annotation["kind"].strip())
        else:
            self.graph.add_property(name, "members", _class_name(member.strip()))

    def _relation(self, found: re.Match[str]) -> None:
        a, b = _class_name(found["a"]), _class_name(found["b"])
        left, right, dashed = found["left"], found["right"], found["line"] == ".."
        marker = left or right
        cardinality = f"{found['ca']} to {found['cb']}" if found["ca"] and found["cb"] else ""
        if marker is None or (left and right):
            label = _text(found["label"]) or ("linked with" if not marker else _MARKERS[marker][dashed])
            self.graph.edge(a, b, _with(label, cardinality), directed=False)
            return
        label = _text(found["label"]) or _MARKERS[marker][dashed]
        # A whole points at its parts; anything else points at its marker.
        toward_marker = marker not in ("*", "o")
        source, target = (b, a) if (left is not None) == toward_marker else (a, b)
        self.graph.edge(source, target, _with(label, cardinality))


def _with(label: str, detail: str) -> str:
    return f"{label} ({detail})" if label and detail else label or detail


# ER diagrams ---------------------------------------------------------------------

_ENTITY_NAME = r'"[^"]+"|[\w-]+'
_ER_RELATION = re.compile(
    rf"^(?P<a>{_ENTITY_NAME})\s*(?P<left>\|o|\|\||\}}o|\}}\|)(?P<line>--|\.\.)(?P<right>o\||\|\||o\{{|\|\{{)"
    rf"\s*(?P<b>{_ENTITY_NAME})\s*:\s*(?P<label>.+)$"
)
_ER_ENTITY = re.compile(rf"^(?P<id>{_ENTITY_NAME})\s*(?:\[(?P<alias>[^\]]*)\])?\s*(?P<open>\{{)?\s*$")
_ATTRIBUTE = re.compile(
    r'^(?P<type>[\w\-\[\](),~]+)\s+(?P<name>[\w\-*]+)(?P<keys>(?:\s+(?:PK|FK|UK)(?:\s*,\s*(?:PK|FK|UK))*)?)\s*(?:"(?P<comment>[^"]*)")?$'
)
_ONE_SIDE = {"|o": "zero or one", "o|": "zero or one", "||": "exactly one"}
_MANY_SIDE = {"}o": "zero or more", "o{": "zero or more", "}|": "one or more", "|{": "one or more"}


class _ErDiagram:
    def __init__(self) -> None:
        self.graph = _Graph()

    def read(self, lines: Sequence[_Line]) -> None:
        entity: str | None = None
        for line in lines:
            statement = _CSS_CLASS.sub("", line.text).strip()
            word = statement.split(maxsplit=1)[0].lower() if statement else ""
            if not statement or word in ("direction", "classdef", "class", "style"):
                continue
            if entity is not None:
                if statement == "}":
                    entity = None
                else:
                    self._attribute(entity, statement)
                continue
            relation = _ER_RELATION.match(statement)
            if relation:
                a, b = _text(relation["a"]), _text(relation["b"])
                how = f"{_side(relation['left'])} to {_side(relation['right'])}"
                self.graph.edge(a, b, _with(_text(relation["label"]), how))
                continue
            declared = _ER_ENTITY.match(statement)
            if declared:
                name = _text(declared["id"])
                self.graph.node(name, _text(declared["alias"]) or None)
                if declared["open"]:
                    entity = name

    def _attribute(self, entity: str, statement: str) -> None:
        found = _ATTRIBUTE.match(statement)
        if found is None:
            self.graph.add_property(entity, "attributes", _text(statement))
            return
        keys = ", ".join(key.strip() for key in found["keys"].replace(",", " ").split())
        detail = ", ".join(part for part in (found["type"], keys) if part)
        text = f"{found['name']} ({detail})"
        if found["comment"]:
            text += f": {_text(found['comment'])}"
        self.graph.add_property(entity, "attributes", text)


def _side(marker: str) -> str:
    return _ONE_SIDE.get(marker) or _MANY_SIDE.get(marker) or marker


# -- Sequence diagrams ---------------------------------------------------------------

_PARTICIPANT = re.compile(
    r"^(?:create\s+)?(?P<type>participant|actor)\s+(?P<id>[^\s@]+)(?:@\{[^}]*\})?(?:\s+as\s+(?P<alias>.+))?$",
    re.IGNORECASE,
)
_MESSAGE = re.compile(
    r"^(?P<a>[^\s:+\-<>][^:]*?)\s*(?P<arrow><<-{1,2}>>|-{1,2}>>|-{1,2}>|-{1,2}x|-{1,2}\))\s*[+-]?\s*"
    r"(?P<b>[^:]+?)\s*(?::\s*(?P<text>.*))?$"
)
_SEQUENCE_NOTE = re.compile(r"^note\s+(?P<where>left of|right of|over)\s+(?P<who>[^:]+?)\s*:\s*(?P<text>.*)$", re.I)
_BLOCK = re.compile(r"^(?P<kind>loop|alt|opt|par|critical|break|rect|box)\b\s*(?P<label>.*)$", re.IGNORECASE)
_BRANCH = re.compile(r"^(?P<kind>else|and|option)\b\s*(?P<label>.*)$", re.IGNORECASE)
_SEQUENCE_SKIP = frozenset(
    {"autonumber", "activate", "deactivate", "destroy", "link", "links", "properties", "details", "create"}
)
_COLOR = re.compile(
    r"^(?:rgba?\([^)]*\)|transparent|#[0-9a-fA-F]{3,8}|aqua|black|blue|gray|green|grey|orange|pink|purple|red|white|yellow)\b\s*",
    re.IGNORECASE,
)


class _SequenceDiagram:
    def __init__(self, section_path: Sequence[str]) -> None:
        self.base = list(section_path)
        self.participants: dict[str, str] = {}
        self.actors: set[str] = set()
        self.blocks: list[str] = []
        self.elements: list[Element] = []
        self.title: str | None = None

    def read(self, lines: Sequence[_Line]) -> None:
        first = lines[0].number if lines else 1
        for line in lines:
            statement = line.text
            word = statement.split(maxsplit=1)[0].lower()
            titled = _TITLE.match(statement)
            if titled:
                self.title = self.title or _text(titled.group(1))
                continue
            participant = _PARTICIPANT.match(statement)
            if participant:
                self._who(participant["id"], _text(participant["alias"]) or None)
                if participant["type"].lower() == "actor":
                    self.actors.add(participant["id"])
                continue
            if word in _SEQUENCE_SKIP:
                continue
            if word == "end":
                if self.blocks:
                    self.blocks.pop()
                continue
            block = _BLOCK.match(statement)
            if block:
                label = block["label"]
                if block["kind"].lower() in ("rect", "box"):
                    label = _COLOR.sub("", label)
                label = _text(label)
                self.blocks.append(
                    f"{block['kind'].lower()} {label}".strip() if block["kind"].lower() != "rect" else label
                )
                continue
            branch = _BRANCH.match(statement)
            if branch and self.blocks:
                self.blocks[-1] = f"{branch['kind'].lower()} {_text(branch['label'])}".strip()
                continue
            note = _SEQUENCE_NOTE.match(statement)
            if note:
                who = ", ".join(self._who(name.strip()) for name in note["who"].split(","))
                text = f"Note {note['where'].lower()} {who}: {_text(note['text'])}"
                self._add(ElementKind.PARAGRAPH, text, line)
                continue
            message = _MESSAGE.match(statement)
            if message:
                source, target = self._who(message["a"].strip()), self._who(message["b"].strip())
                text = f"{source} → {target}"
                if message["arrow"].endswith(")"):
                    text += " (async)"
                if message["text"] and _text(message["text"]):
                    text += f": {_text(message['text'])}"
                self._add(ElementKind.LIST_ITEM, text, line)
        if self.participants:
            names = [f"{label} (actor)" if key in self.actors else label for key, label in self.participants.items()]
            self.elements.insert(
                0,
                Element(
                    kind=ElementKind.PARAGRAPH,
                    text="Participants: " + ", ".join(names),
                    section_path=list(self.base),
                    location=SourceLocation(line_start=first, line_end=first),
                ),
            )

    def _who(self, key: str, label: str | None = None) -> str:
        if label or key not in self.participants:
            self.participants[key] = label or self.participants.get(key) or _text(key)
        return self.participants[key]

    def _add(self, kind: ElementKind, text: str, line: _Line) -> None:
        self.elements.append(
            Element(
                kind=kind,
                text=text,
                section_path=[*self.base, *(block for block in self.blocks if block)],
                location=SourceLocation(line_start=line.number, line_end=line.number),
            )
        )


# -- Reading a diagram -----------------------------------------------------------------


class _GraphReader(Protocol):
    graph: _Graph

    def read(self, lines: Sequence[_Line]) -> None: ...


_GRAPHS: dict[str, Callable[[], _GraphReader]] = {
    "flowchart": _Flowchart,
    "state": _StateDiagram,
    "class": _ClassDiagram,
    "er": _ErDiagram,
}


@dataclass
class Diagram:
    """What a Mermaid diagram says, as elements."""

    # flowchart, sequence, class, state, er; else Mermaid's own name, e.g. gantt.
    kind: str
    # From its front matter, title statement or accessible title.
    title: str | None
    elements: list[Element]
    # Its front matter's other scalar settings.
    metadata: dict[str, Any] = field(default_factory=dict)


def read_diagram(
    text: str,
    *,
    section_path: Sequence[str] = (),
    name: str | None = None,
    first_line: int = 1,
) -> Diagram:
    """
    Read a Mermaid diagram.

    :param text: Its source.
    :param section_path: The sections it's in, e.g. a Markdown document's.
    :param name: What to call a graph without a title of its own.
    :param first_line: The line its source starts on, for locations.
    :return: The diagram: never without elements unless its source is blank.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    front, body, consumed = _front_matter(text)
    title = _scalar(front.get("title"))
    acc_title = _ACC_TITLE.search(body)
    descriptions = [_text(m.group(1) or m.group(2)) for m in _ACC_DESCR.finditer(body)]
    body = _ACC_TITLE.sub("", body)
    body = _ACC_DESCR.sub(_blank, body)
    body = _DIRECTIVE.sub(_blank, body)
    body = _COMMENT.sub("", body)
    lines = _statements(body, first_line + consumed)
    title = title or (_text(acc_title.group(1)) if acc_title else None)
    metadata = {key: value for key, value in front.items() if key not in ("title", "config") and _plain(value)}
    if not lines:
        return Diagram("empty", title, [], metadata)

    word, _, options = lines[0].text.partition(" ")
    kind = _KINDS.get(word.lower(), word)
    # e.g. "pie title Pets adopted"
    titled = _TITLE.match(options.strip())
    title = title or (_text(titled.group(1)) if titled else None)
    path = list(section_path)
    whole = SourceLocation(line_start=lines[0].number, line_end=lines[-1].number)
    elements = [
        Element(kind=ElementKind.PARAGRAPH, text=description, section_path=path, location=whole)
        for description in descriptions
        if description
    ]
    rest = lines[1:]
    read = False
    if kind == "sequence":
        sequence = _SequenceDiagram(path)
        sequence.read(rest)
        title = title or sequence.title
        elements.extend(sequence.elements)
        read = bool(sequence.elements)
    elif kind in _GRAPHS:
        reader = _GRAPHS[kind]()
        reader.read(rest)
        graph = reader.graph.data(title or name or f"{kind} diagram")
        if graph is not None:
            elements.append(Element(kind=ElementKind.GRAPH, graph=graph, section_path=path, location=whole))
            read = True
        if isinstance(reader, _ClassDiagram):
            elements.extend(
                Element(
                    kind=ElementKind.PARAGRAPH,
                    text=f"Note: {note}",
                    section_path=path,
                    location=SourceLocation(line_start=line.number, line_end=line.number),
                )
                for note, line in reader.notes
                if note
            )
    else:
        title = title or next((_text(m.group(1)) for line in rest if (m := _TITLE.match(line.text))), None)
    if not read:
        # Nothing read of it: its source is what's searched.
        elements.append(
            Element(kind=ElementKind.CODE, text=body.strip(), language="mermaid", section_path=path, location=whole)
        )
    return Diagram(kind, title, elements, metadata)


def _front_matter(text: str) -> tuple[dict[str, Any], str, int]:
    found = _FRONT_MATTER.match(text)
    if not found:
        return {}, text, 0
    data: dict[str, Any] = {}
    try:
        import yaml

        loaded = yaml.safe_load(found.group(1))
        if isinstance(loaded, dict):
            data = {str(key): value for key, value in loaded.items()}
    except Exception:
        for line in found.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep and not line.startswith((" ", "\t")):
                data[key.strip()] = value.strip().strip("'\"")
    consumed = text[: found.end()]
    return data, text[found.end() :], consumed.count("\n")


def _scalar(value: Any) -> str | None:
    if not isinstance(value, str | int | float):
        return None
    return _text(str(value)) or None


def _plain(value: Any) -> bool:
    if isinstance(value, str | int | float | bool):
        return True
    return isinstance(value, list) and all(isinstance(item, str | int | float | bool) for item in value)


class MermaidParser:
    name = "mermaid"
    version = "1"
    extensions = frozenset({"mmd", "mermaid"})
    media_types = frozenset({"text/vnd.mermaid", "text/x-mermaid", "application/vnd.mermaid"})

    def parse(self, source: SourceFile) -> ParsedDocument:
        text = decode_bytes(source.data)
        fallback = title_from_filename(source.filename)
        diagram = read_diagram(text, name=fallback)
        return ParsedDocument(
            tenant_id=source.tenant_id,
            doc_id=source.doc_id,
            source_type="mermaid",
            title=diagram.title or fallback,
            elements=diagram.elements,
            parser_name=self.name,
            parser_version=self.version,
            metadata={"diagram": diagram.kind, **diagram.metadata},
        )
