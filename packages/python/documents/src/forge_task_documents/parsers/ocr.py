"""OCR for PDF pages with no text layer (scans, text turned into outlines).

The PDF parser renders such a page to an image and hands it to an
``OcrEngine``; the words come back with their boxes in the image's pixels,
and the parser lays them out exactly like a text layer's words, so headings,
columns, paragraphs and tables are found the same way.

``TesseractOcr`` runs the ``tesseract`` command (a system package: apt
``tesseract-ocr`` plus a ``tesseract-ocr-<lang>`` pack per extra language,
brew ``tesseract``) and reads its TSV output.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from forge_task_documents.errors import ParseError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OcrWord:
    """A recognised word, in pixels of the page image it was read from."""

    text: str
    left: float
    top: float
    right: float
    bottom: float
    # Its line's box height: the size its text is set at, give or take.
    line_height: float
    conf: float = 100.0


@runtime_checkable
class OcrEngine(Protocol):
    name: str

    def read(self, image: Any, *, dpi: int) -> list[OcrWord]:
        """The words on a page image (a PIL image rendered at ``dpi``)."""
        ...


class TesseractOcr:
    name = "tesseract"

    def __init__(
        self,
        command: str = "tesseract",
        *,
        languages: str = "eng",
        timeout_s: float = 120.0,
        min_confidence: float = 30.0,
    ) -> None:
        self.command = command
        self.languages = languages
        self.timeout_s = timeout_s
        self.min_confidence = min_confidence

    @classmethod
    def find(cls, command: str = "tesseract", **options: Any) -> TesseractOcr | None:
        """The engine, when the command is installed; None otherwise."""
        path = shutil.which(command)
        return cls(path, **options) if path else None

    def read(self, image: Any, *, dpi: int) -> list[OcrWord]:
        with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
            path = os.path.join(tmp, "page.png")
            image.save(path)
            args = [self.command, path, "stdout", "-l", self.languages, "--dpi", str(dpi), "--psm", "3", "tsv"]
            try:
                # One thread per page: the worker already runs documents side by side.
                env = {**os.environ, "OMP_THREAD_LIMIT": os.environ.get("OMP_THREAD_LIMIT", "1")}
                done = subprocess.run(args, capture_output=True, timeout=self.timeout_s, check=False, env=env)
            except subprocess.TimeoutExpired as exc:
                raise ParseError(f"tesseract took over {self.timeout_s:g}s on a page") from exc
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()[-300:]
            raise ParseError(f"tesseract failed ({done.returncode}): {detail}")
        return parse_tsv(done.stdout.decode("utf-8", "replace"), min_confidence=self.min_confidence)


def parse_tsv(tsv: str, *, min_confidence: float = 30.0) -> list[OcrWord]:
    """Words from Tesseract's TSV: level 4 rows are lines, level 5 rows words."""
    lines: dict[tuple[str, ...], float] = {}
    rows: list[list[str]] = []
    for raw in tsv.splitlines()[1:]:  # the first line is the header
        cols = raw.split("\t")
        if len(cols) < 12:
            continue
        key = tuple(cols[1:5])  # page, block, paragraph, line
        if cols[0] == "4":
            lines[key] = float(cols[9])
        elif cols[0] == "5":
            rows.append(cols)
    words: list[OcrWord] = []
    for cols in rows:
        text = cols[11].strip()
        conf = float(cols[10])
        if not text or conf < min_confidence:
            continue
        left, top, width, height = (float(c) for c in cols[6:10])
        words.append(
            OcrWord(
                text=text,
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
                line_height=lines.get(tuple(cols[1:5]), height),
                conf=conf,
            )
        )
    return words
