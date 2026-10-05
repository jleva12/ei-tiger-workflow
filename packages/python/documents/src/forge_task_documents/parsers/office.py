"""Legacy and OpenDocument office files, read by converting them first.

Binary Office 97-2003 files (.ppt, .doc, .xls), RTF and OpenDocument (.odt,
.ods, .odp) are converted with LibreOffice (``soffice --headless
--convert-to``) to the OOXML format of the same kind, which the registry's
parser for that format then reads: a .ppt comes out exactly like a .pptx.

LibreOffice is a system package the deployment installs; without it these
formats fail with an error saying so.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from forge_task_documents.errors import ParseError
from forge_task_documents.models import ParsedDocument, SourceFile

if TYPE_CHECKING:
    from forge_task_documents.protocols import Parser

log = logging.getLogger(__name__)

# What each format is converted to.
TARGETS: dict[str, str] = {
    "ppt": "pptx",
    "pps": "pptx",
    "pot": "pptx",
    "odp": "pptx",
    "doc": "docx",
    "dot": "docx",
    "rtf": "docx",
    "odt": "docx",
    "xls": "xlsx",
    "xlt": "xlsx",
    "ods": "xlsx",
}

_MEDIA_TYPES: dict[str, str] = {
    "application/vnd.ms-powerpoint": "ppt",
    "application/msword": "doc",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.ms-excel": "xls",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
}


class OfficeConverter:
    """Runs LibreOffice headless, one conversion per call.

    Each call gets its own LibreOffice profile, so conversions running at the
    same time (worker threads, processes) don't wait on each other's lock."""

    def __init__(self, command: str = "soffice", *, timeout_s: float = 180.0) -> None:
        self.command = command
        self.timeout_s = timeout_s

    @classmethod
    def find(cls, command: str = "soffice", **options: float) -> OfficeConverter | None:
        """The converter, when LibreOffice is installed; None otherwise."""
        for candidate in (command, "soffice", "libreoffice"):
            path = shutil.which(candidate)
            if path:
                return cls(path, **options)
        return None

    def convert(self, data: bytes, extension: str, target: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="office-") as tmp:
            root = Path(tmp)
            source = root / f"source.{extension}"
            source.write_bytes(data)
            out = root / "out"
            args = [
                self.command,
                f"-env:UserInstallation={(root / 'profile').as_uri()}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--convert-to",
                target,
                "--outdir",
                str(out),
                str(source),
            ]
            try:
                done = subprocess.run(
                    args, capture_output=True, timeout=self.timeout_s, check=False, env={**os.environ, "HOME": tmp}
                )
            except subprocess.TimeoutExpired as exc:
                raise ParseError(f"LibreOffice took over {self.timeout_s:g}s to convert .{extension}") from exc
            converted = out / f"source.{target}"
            if done.returncode != 0 or not converted.exists():
                detail = (done.stderr or done.stdout).decode("utf-8", "replace").strip()[-300:]
                raise ParseError(f"LibreOffice could not convert .{extension} to .{target}: {detail or 'no output'}")
            return converted.read_bytes()


class OfficeConvertingParser:
    """Parses legacy and OpenDocument office files by converting them to the
    OOXML format of the same kind and handing that to ``resolve``: the
    registry's own lookup, so a swapped docx parser (Docling) applies too."""

    name = "libreoffice"
    version = "1"
    extensions = frozenset(TARGETS)
    media_types = frozenset(_MEDIA_TYPES)

    def __init__(self, converter: OfficeConverter | None, resolve: Callable[[SourceFile], Parser]) -> None:
        self.converter = converter
        self.resolve = resolve

    def parse(self, source: SourceFile) -> ParsedDocument:
        extension = source.extension if source.extension in TARGETS else self._sniffed(source)
        target = TARGETS[extension]
        if self.converter is None:
            raise ParseError(
                f"cannot read .{extension} file {source.filename!r}: it is converted with LibreOffice, which is not "
                f"installed (no soffice on PATH). Save it as .{target}, or install LibreOffice"
            )
        data = self.converter.convert(source.data, extension, target)
        stem = PurePosixPath(source.filename).stem or "document"
        converted = source.model_copy(update={"data": data, "filename": f"{stem}.{target}", "media_type": None})
        parser = self.resolve(converted)
        if parser is self:  # pragma: no cover - a registry mapping the target back here
            raise ParseError(f"no parser for .{target}, which .{extension} is converted to")
        doc = parser.parse(converted)
        return doc.model_copy(
            update={
                "parser_name": f"{self.name}+{doc.parser_name}",
                "parser_version": f"{self.version}+{doc.parser_version}",
                "metadata": {**doc.metadata, "converted_from": extension},
            }
        )

    @staticmethod
    def _sniffed(source: SourceFile) -> str:
        from forge_task_documents.parsers.registry import sniff_extension

        mt = (source.media_type or "").split(";")[0].strip().lower()
        extension = _MEDIA_TYPES.get(mt) or sniff_extension(source.data) or ""
        if extension not in TARGETS:
            raise ParseError(f"cannot tell which office format {source.filename!r} is")
        return extension
