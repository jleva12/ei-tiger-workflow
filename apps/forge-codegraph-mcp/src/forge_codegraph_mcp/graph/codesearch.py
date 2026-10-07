"""How search reads a query and the documents it searches.

A port of the worker's ``codesearch`` package (packages/go/code-graph/domain):
the query parser, the lexical tokenizer, identifier splitting, snippets, and
the retrieval document a node is indexed as. Search runs against the index
the worker built with the Go versions, so these must agree with them: change
both together.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from forge_codegraph_mcp.graph.model import jfield, prop

DOCUMENT_VERSION = 1
MAX_TERMS = 24
MAX_QUERY_SYMBOLS = 8
MAX_QUERY_PATHS = 4

# English function and question words plus the generic code vocabulary an
# agent uses to describe what it wants. They carry no signal against a code
# document, and the kind words appear in every document header.
STOP_WORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "every",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "let",
        "may",
        "me",
        "might",
        "more",
        "most",
        "must",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "please",
        "same",
        "shall",
        "she",
        "should",
        "show",
        "so",
        "some",
        "such",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "code",
        "codebase",
        "source",
        "file",
        "files",
        "folder",
        "directory",
        "project",
        "repository",
        "repo",
        "class",
        "classes",
        "interface",
        "interfaces",
        "enum",
        "enums",
        "record",
        "records",
        "constructor",
        "constructors",
        "method",
        "methods",
        "function",
        "functions",
        "field",
        "fields",
        "type",
        "types",
        "module",
        "modules",
        "package",
        "packages",
        "definition",
        "definitions",
        "define",
        "defined",
        "defines",
        "declaration",
        "declarations",
        "declared",
        "implementation",
        "implementations",
        "implement",
        "implemented",
        "implementing",
        "implements",
        "use",
        "used",
        "uses",
        "using",
        "usage",
        "call",
        "calls",
        "called",
        "caller",
        "callers",
        "callee",
        "callees",
        "invoke",
        "invokes",
        "invoked",
        "reference",
        "references",
        "referenced",
        "referencing",
        "find",
        "list",
        "explain",
        "describe",
        "give",
        "want",
        "need",
        "looking",
        "look",
    ]
)

SOURCE_EXTENSIONS = (
    ".java", ".kt", ".scala", ".groovy", ".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".cs",
    ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".xml", ".yaml", ".yml", ".json",
    ".toml", ".properties", ".gradle", ".sql", ".md", ".proto",
)  # fmt: skip

# The node kinds a retrieval document is made for.
DOCUMENT_KINDS = frozenset(
    {
        "class", "interface", "enum", "record", "annotation_type", "type_alias", "namespace",
        "method", "constructor", "function", "field", "variable", "enum_constant",
        "initializer", "code_chunk",
    }
)  # fmt: skip

_DOCUMENT_FIELDS = (
    ("file_path", "File"),
    ("language", "Language"),
    ("owner_key", "Owner"),
    ("signature", "Signature"),
    ("canonical_signature", "Resolved signature"),
    ("docstring", "Documentation"),
    ("members", "Members"),
    ("source_text", "Code"),
)


def is_letter(c: str) -> bool:
    return unicodedata.category(c).startswith("L")


def is_number(c: str) -> bool:
    return unicodedata.category(c).startswith("N")


def is_upper(c: str) -> bool:
    return unicodedata.category(c) == "Lu"


def is_lower(c: str) -> bool:
    return unicodedata.category(c) == "Ll"


def _word_char(c: str) -> bool:
    return is_letter(c) or is_number(c) or c == "_"


def _fields(value: str, keep: Callable[[str], bool]) -> list[str]:
    """Runs of characters ``keep`` accepts, as Go's strings.FieldsFunc splits
    on the rest."""
    out: list[str] = []
    start = -1
    for i, c in enumerate(value):
        if keep(c):
            if start < 0:
                start = i
        elif start >= 0:
            out.append(value[start:i])
            start = -1
    if start >= 0:
        out.append(value[start:])
    return out


@dataclass(slots=True)
class Query:
    """What search understood from free text: an identifier, a partial
    identifier, or a question that names symbols and paths in passing."""

    text: str = jfield("")
    # Natural language: three or more words.
    sentence: bool = jfield(False)
    # The lexical terms the search index evaluates; stop words are removed
    # from sentences.
    terms: list[str] = jfield(factory=list, omitempty=True)
    # Identifier-shaped words as written, each looked up by exact name.
    symbols: list[str] = jfield(factory=list, omitempty=True)
    # Words that look like file paths; hits under them rank higher.
    paths: list[str] = jfield(factory=list, omitempty=True)


def parse_query(text: str) -> Query:
    """Classifies free text for retrieval, deterministically and without
    consulting the graph."""
    q = Query(text=text.strip())
    words = q.text.split()
    q.sentence = len(words) >= 3
    for raw in words:
        word = trim_word(raw)
        if not word:
            continue
        if path_like(word):
            if word not in q.paths and len(q.paths) < MAX_QUERY_PATHS:
                q.paths.append(word)
        elif symbol_like(word) and word not in q.symbols and len(q.symbols) < MAX_QUERY_SYMBOLS:
            q.symbols.append(word)
    if not q.sentence:
        q.terms = terms(q.text)
        return q
    q.terms = collect_terms(q.text, lambda term: term not in STOP_WORDS)
    if not q.terms:
        q.terms = terms(q.text)
    return q


def simple_name(symbol: str) -> str:
    """The last dotted segment of a symbol before any parameter list:
    "com.acme.Foo", "Foo.bar()" and "bar(com.acme.Foo)" give Foo, bar and bar."""
    symbol = symbol.split("(", 1)[0]
    return symbol.rsplit(".", 1)[-1]


def owner(symbol: str) -> str:
    """The type a member reference names ("OrderService.placeOrder" gives
    OrderService); lower-case segments are packages, not owners."""
    segments = symbol.split("(", 1)[0].split(".")
    if len(segments) < 2:
        return ""
    candidate = segments[-2]
    if not candidate or not is_upper(candidate[0]):
        return ""
    return candidate


_TRIMMED = "\"'`,;:?![]{}<>"


def trim_word(word: str) -> str:
    """Strips the punctuation a sentence wraps around a symbol or path, keeping
    parentheses that belong to it ("save()")."""
    word = word.strip(_TRIMMED).rstrip(".")
    if word.startswith("(") and "(" not in word[1:]:
        word = word.removeprefix("(").removesuffix(")")
    return word


def path_like(word: str) -> bool:
    """A word that names a file or directory: a path separator outside a URL,
    or a source file extension."""
    if "://" in word:
        return False
    if "/" in word:
        return bool(word.strip("/"))
    lower = word.lower()
    return any(lower.endswith(ext) and len(lower) > len(ext) for ext in SOURCE_EXTENSIONS)


def symbol_like(word: str) -> bool:
    """A word that spells a symbol: camelCase, dotted or qualified, snake_case,
    or a call with parentheses. Plain words, numbers and abbreviations are not."""
    if len(word.encode()) < 2:
        return False
    letters = 0
    for c in word:
        if is_letter(c):
            letters += 1
        elif not is_number(c) and c not in "._$():<>[]":
            return False
    if letters == 0:
        return False
    if any(c in word for c in "._$("):
        return word.strip("._$()") != ""
    lower = any(is_lower(c) for c in word)
    upper = any(is_upper(c) for c in word)
    inner_upper = any(is_upper(c) for c in word[1:])
    if not (lower and upper):
        return False
    # Capitalised only at its start: a type name, unless a sentence opener.
    return inner_upper or word.lower() not in STOP_WORDS


def terms(value: str) -> list[str]:
    """Lower-case lexical terms: runs of letters, digits and underscores of at
    least two bytes, deduplicated, at most 24."""
    return collect_terms(value, None)


def collect_terms(value: str, keep: Callable[[str], bool] | None) -> list[str]:
    out: list[str] = []
    for term in _fields(value.lower(), _word_char):
        if len(term.encode()) < 2 or term in out or (keep is not None and not keep(term)):
            continue
        out.append(term)
        if len(out) == MAX_TERMS:
            break
    return out


def identifier_terms(name: str, qualified_name: str) -> str:
    """The lexical forms of a symbol name: as written, and every dotted,
    camelCase, snake_case and digit-boundary fragment of it."""
    out: list[str] = []

    def add(value: str) -> None:
        if value and value not in out:
            out.append(value)

    for source in (name, qualified_name):
        add(source)
        for segment in _fields(source, _word_char):
            add(segment)
            for fragment in split_identifier(segment):
                add(fragment)
    return " ".join(out)


def _char_kind(c: str) -> int:
    if is_upper(c):
        return 1
    if is_lower(c):
        return 2
    if is_number(c):
        return 3
    return 0


def split_identifier(identifier: str) -> list[str]:
    """An identifier's camelCase, snake_case and digit-boundary fragments:
    "runAsyncImpl" is run, Async, Impl; "HTTPServer2" is HTTP, Server, 2."""
    out: list[str] = []
    chars = identifier
    start = 0

    def flush(end: int) -> None:
        nonlocal start
        if end > start:
            out.append(chars[start:end])
        start = end

    for i in range(1, len(chars) + 1):
        if i == len(chars):
            flush(i)
            break
        prev, cur = _char_kind(chars[i - 1]), _char_kind(chars[i])
        if cur == 0:
            flush(i)
            start = i + 1
        elif prev == 0:
            continue
        elif prev != cur and not (prev == 1 and cur == 2):
            flush(i)
        elif prev == 1 and cur == 2 and i - 1 > start and _char_kind(chars[i - 2]) == 1:
            # "HTTPServer": the last capital starts the next word.
            flush(i - 1)
    return out


def _rune_start(data: bytes, i: int) -> bool:
    return data[i] & 0xC0 != 0x80


def snippet(value: str, query_terms: Iterable[str], max_bytes: int) -> str:
    """A bounded window of text around the first occurrence of any term, on
    whole lines where possible."""
    if max_bytes <= 0 or not value:
        return ""
    data = value.encode()
    lower = value.lower().encode()
    at = -1
    for term in query_terms:
        i = lower.find(term.encode())
        if i >= 0 and (at < 0 or i < at):
            at = i
    at = max(at, 0)
    start = max(at - max_bytes // 3, 0)
    newline = data.rfind(b"\n", 0, at)
    if newline >= 0 and newline >= start:
        start = newline + 1
    end = start + max_bytes
    if end >= len(data):
        end = len(data)
    else:
        newline = data.find(b"\n", end)
        if newline >= 0 and newline - end < max_bytes // 4:
            end = newline
        while end > start and not _rune_start(data, end):
            end -= 1
    while start < end and not _rune_start(data, start):
        start += 1
    return data[start:end].decode(errors="replace").strip()


@dataclass(frozen=True, slots=True)
class Document:
    """A node's retrieval document: the text the worker indexed and embedded,
    and its hash, which tells whether an index row still describes the node."""

    node_id: str
    text: str
    hash: str
    version: int = DOCUMENT_VERSION


def document_of(node: dict[str, Any]) -> Document | None:
    """The retrieval document of a node, or None for kinds that have none.
    Location-only changes leave it unchanged."""
    kind = str(node.get("kind") or "")
    if kind not in DOCUMENT_KINDS:
        return None
    parts = [f"{kind} {node.get('name') or ''}\nQualified name: {node.get('qualified_name') or ''}"]
    for key, label in _DOCUMENT_FIELDS:
        if value := prop(node, key):
            parts.append(f"\n{label}:\n{value}")
    body = "".join(parts)
    digest = hashlib.sha256(f"code-document-v{DOCUMENT_VERSION}\n{body}".encode()).hexdigest()
    return Document(node_id=str(node.get("id") or ""), text=body, hash=digest)
