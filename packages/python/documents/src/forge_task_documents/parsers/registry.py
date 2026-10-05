"""Parser registry: resolves a SourceFile to the Parser that handles it.

Resolution order: explicit file extension -> declared media type -> content
sniffing (the PDF header, OOXML and OpenDocument zip parts, the stream names of
a legacy OLE file, RTF, then "is this decodable text?").

Third-party parsers can be added without touching this package by exposing an
entry point in the ``forge_task_documents.parsers`` group that points at a zero-arg
factory (or class) returning a Parser.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from forge_task_documents.errors import UnsupportedFormatError
from forge_task_documents.models import SourceFile

if TYPE_CHECKING:
    from forge_task_documents.protocols import Parser

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "forge_task_documents.parsers"

# Zip part that identifies each OOXML flavour.
_OOXML_MARKERS: dict[str, str] = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "visio/document.xml": "vsdx",
    "ppt/presentation.xml": "pptx",
}

# The main stream each legacy Office 97-2003 (OLE) format keeps, as its UTF-16 name.
_OLE_STREAMS: dict[bytes, str] = {
    "PowerPoint Document".encode("utf-16-le"): "ppt",
    "WordDocument".encode("utf-16-le"): "doc",
    "Workbook".encode("utf-16-le"): "xls",
    "VisioDocument".encode("utf-16-le"): "vsd",
}

_ODF_TYPES: dict[bytes, str] = {
    b"application/vnd.oasis.opendocument.text": "odt",
    b"application/vnd.oasis.opendocument.spreadsheet": "ods",
    b"application/vnd.oasis.opendocument.presentation": "odp",
}


class ParserRegistry:
    def __init__(self, parsers: Iterable[Parser] = ()) -> None:
        self._by_ext: dict[str, Parser] = {}
        self._by_media: dict[str, Parser] = {}
        self._parsers: dict[str, Parser] = {}
        for p in parsers:
            self.register(p)

    def register(self, parser: Parser, *, override: bool = True) -> None:
        """Later registrations win, so you can swap e.g. the docx parser."""
        for ext in parser.extensions:
            ext = ext.lower().lstrip(".")
            if not override and ext in self._by_ext:
                continue
            self._by_ext[ext] = parser
        for mt in parser.media_types:
            if not override and mt in self._by_media:
                continue
            self._by_media[mt.lower()] = parser
        self._parsers[parser.name] = parser

    def load_entry_points(self) -> int:
        count = 0
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                factory = ep.load()
                self.register(factory())
                count += 1
            except Exception:  # a broken plugin must not take the worker down
                log.exception("failed to load parser entry point %s", ep.name)
        return count

    @property
    def parsers(self) -> list[Parser]:
        return list(self._parsers.values())

    def supported_extensions(self) -> list[str]:
        return sorted(self._by_ext)

    def resolve(self, source: SourceFile) -> Parser:
        if source.extension and source.extension in self._by_ext:
            return self._by_ext[source.extension]
        if source.media_type:
            mt = source.media_type.split(";")[0].strip().lower()
            if mt in self._by_media:
                return self._by_media[mt]
        sniffed = sniff_extension(source.data)
        if sniffed and sniffed in self._by_ext:
            return self._by_ext[sniffed]
        raise UnsupportedFormatError(
            f"no parser for {source.filename!r} (ext={source.extension!r}, "
            f"media_type={source.media_type!r}, sniffed={sniffed!r})"
        )


def sniff_extension(data: bytes) -> str | None:
    if b"%PDF-" in data[:1024]:  # readers accept junk before the header
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            import io

            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return None
        for marker, ext in _OOXML_MARKERS.items():
            if marker in names:
                return ext
        if "mimetype" in names:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return _ODF_TYPES.get(zf.read("mimetype").strip())
        return None
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # legacy OLE: named by its main stream, found in the directory
        for stream, ext in _OLE_STREAMS.items():
            if stream in data:
                return ext
        return None
    if data[:5] == b"{\\rtf":
        return "rtf"
    sample = data[:4096]
    if b"\x00" in sample:
        return None
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "txt"
