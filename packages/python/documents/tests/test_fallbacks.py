"""The parsers' fallbacks: OCR for scanned PDF pages, tables found by
alignment when a PDF draws no rules, and legacy or OpenDocument office files
converted with LibreOffice. Tests marked for Tesseract or LibreOffice run
only where those are installed (the worker image)."""

from __future__ import annotations

import pytest

from forge_task_documents.errors import ParseError
from forge_task_documents.models import ElementKind
from forge_task_documents.parsers import (
    OfficeConverter,
    OfficeConvertingParser,
    PdfParser,
    TesseractOcr,
    sniff_extension,
)
from forge_task_documents.parsers.ocr import OcrWord, parse_tsv

from . import fixtures
from .conftest import source

needs_tesseract = pytest.mark.skipif(TesseractOcr.find() is None, reason="tesseract is not installed")
needs_libreoffice = pytest.mark.skipif(OfficeConverter.find() is None, reason="LibreOffice is not installed")


def parse(parser, name: str, data: bytes):
    return parser.parse(source(name, data))


def text_of(doc) -> str:
    return " ".join(e.text for e in doc.elements)


class FakeOcr:
    """Reads fixtures.SCAN_LINES back, where make_scan_image draws them."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[tuple[int, int], int]] = []

    def read(self, image, *, dpi: int) -> list[OcrWord]:
        self.calls.append((image.size, dpi))
        if self.fail:
            raise RuntimeError("engine crashed")
        words: list[OcrWord] = []
        y = 300
        for text, px in fixtures.SCAN_LINES:
            x = 300.0
            for word in text.split():
                width = len(word) * px * 0.5
                words.append(OcrWord(word, x, y, x + width, y + px, line_height=px))
                x += width + px * 0.3
            y += int(px * 1.8)
        return words


# --------------------------------------------------------------------------- OCR


def test_scanned_pdf_is_read_by_ocr():
    ocr = FakeOcr()
    doc = parse(PdfParser(ocr=ocr), "scan.pdf", fixtures.make_pdf_scanned())
    ((width, height), dpi) = ocr.calls[0]
    assert len(ocr.calls) == 1 and dpi == 300 and width == 2550 and abs(height - 3300) <= 1  # letter at 300 dpi
    assert doc.metadata["ocr_pages"] == [1]
    assert doc.title == "Refund Operations"  # the largest line, as on any page
    (para,) = [e for e in doc.elements if e.kind is ElementKind.PARAGRAPH]
    assert para.text == (
        "Customers may request a refund within 30 days of purchase. Refunds are issued to the original payment method."
    )
    assert para.location.page == 1


def test_only_pages_without_text_are_ocred():
    ocr = FakeOcr()
    doc = parse(PdfParser(ocr=ocr), "mixed.pdf", fixtures.make_pdf_scanned(with_text_page=True))
    assert len(ocr.calls) == 1 and doc.metadata["ocr_pages"] == [2]
    assert "real text layer" in text_of(doc) and "30 days" in text_of(doc)


def test_ocr_page_budget():
    doc = parse(PdfParser(ocr=FakeOcr(), max_ocr_pages=1), "long.pdf", fixtures.make_pdf_scanned(pages=3))
    assert doc.metadata["ocr_pages"] == [1] and doc.metadata["ocr_skipped_pages"] == [2, 3]
    with pytest.raises(ParseError, match="over the OCR limit of 0 pages"):
        parse(PdfParser(ocr=FakeOcr(), max_ocr_pages=0), "long.pdf", fixtures.make_pdf_scanned())


def test_ocr_failures_and_no_engine():
    with pytest.raises(ParseError, match="no text in pdf .*OCR failed .*engine crashed"):
        parse(PdfParser(ocr=FakeOcr(fail=True)), "scan.pdf", fixtures.make_pdf_scanned())
    with pytest.raises(ParseError, match="no OCR engine is installed"):
        parse(PdfParser(ocr=None), "scan.pdf", fixtures.make_pdf_scanned())


def test_text_pages_never_reach_ocr(files):
    ocr = FakeOcr()
    doc = parse(PdfParser(ocr=ocr), "refund_guide.pdf", files["refund_guide.pdf"])
    assert ocr.calls == [] and "ocr_pages" not in doc.metadata


def test_tesseract_tsv():
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "4\t1\t1\t1\t1\t0\t100\t200\t400\t50\t-1\t",
            "5\t1\t1\t1\t1\t1\t100\t205\t120\t40\t96.5\tRefund",
            "5\t1\t1\t1\t1\t2\t240\t210\t90\t35\t12.0\t~~",
            "5\t1\t1\t1\t1\t3\t340\t205\t160\t40\t91.0\tpolicy",
            "5\t1\t1\t1\t1\t4\t520\t205\t10\t40\t95.0\t ",
        ]
    )
    words = parse_tsv(tsv, min_confidence=30)
    assert [(w.text, w.left, w.right, w.line_height) for w in words] == [
        ("Refund", 100, 220, 50),
        ("policy", 340, 500, 50),
    ]


@needs_tesseract
def test_tesseract_reads_a_scan():
    doc = parse(PdfParser(ocr=TesseractOcr.find()), "scan.pdf", fixtures.make_pdf_scanned())
    assert doc.metadata["ocr_pages"] == [1]
    text = text_of(doc) + " " + doc.title
    assert "Refund Operations" in text and "30 days of purchase" in text


# --------------------------------------------------------------------------- borderless tables


def test_borderless_table_found_by_alignment(registry):
    doc = parse(registry.resolve(source("t.pdf", b"")), "limits.pdf", fixtures.make_pdf_borderless_table())
    (table,) = [e.table for e in doc.elements if e.kind is ElementKind.TABLE]
    assert table.headers == ["Role", "Region", "Max refund"]
    assert table.rows == [
        ["Agent", "US", "500"],
        ["Team lead", "US", "5000"],
        ["Agent", "EU", "400 per order, 2000 per day"],  # the wrapped cell carried on
        ["Director", "Global", "No limit"],
    ]
    paragraphs = [e.text for e in doc.elements if e.kind is ElementKind.PARAGRAPH]
    assert paragraphs == [
        "Refund limits differ by role and region, as the table below shows.",
        "Anything above these limits needs approval from finance.",
    ]


def test_prose_is_not_a_borderless_table(registry, files):
    for name in ("refund_guide.pdf",):
        doc = parse(registry.resolve(source(name, b"")), name, files[name])
        assert sum(e.kind is ElementKind.TABLE for e in doc.elements) == 1  # only the ruled one
    clauses = parse(registry.resolve(source("t.pdf", b"")), "terms.pdf", fixtures.make_pdf_clauses())
    assert not any(e.kind is ElementKind.TABLE for e in clauses.elements)


# --------------------------------------------------------------------------- legacy office files


class FakeConverter:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def convert(self, data: bytes, extension: str, target: str) -> bytes:
        self.calls.append((extension, target))
        return self.output


def test_legacy_ppt_is_converted_then_parsed_as_pptx(registry, files):
    converter = FakeConverter(files["review.pptx"])
    registry.register(OfficeConvertingParser(converter, registry.resolve))  # type: ignore[arg-type]
    src = source("review.ppt", b"\xd0\xcf\x11\xe0 legacy")
    parser = registry.resolve(src)
    doc = parser.parse(src)
    assert parser.name == "libreoffice" and converter.calls == [("ppt", "pptx")]
    assert doc.source_type == "pptx" and doc.parser_name == "libreoffice+pptx"
    assert doc.metadata["converted_from"] == "ppt" and doc.title == "Refund Operations Review"
    assert "Approval limits" in [e.text for e in doc.elements if e.kind is ElementKind.HEADING]


def test_converted_files_use_the_registrys_parser(registry, files):
    class MyDocx:
        name = "my-docx"
        version = "7"
        extensions = frozenset({"docx"})
        media_types = frozenset()

        def parse(self, src):
            from forge_task_documents.parsers import DocxParser

            return DocxParser().parse(src).model_copy(update={"parser_name": self.name})

    registry.register(MyDocx())
    registry.register(OfficeConvertingParser(FakeConverter(files["handbook.docx"]), registry.resolve))  # type: ignore[arg-type]
    doc = parse(registry.resolve(source("a.doc", b"")), "handbook.doc", b"legacy")
    assert doc.parser_name == "libreoffice+my-docx" and doc.parser_version == "1+1"
    assert doc.title == "Refund Operations Handbook"


def test_legacy_file_without_libreoffice(registry):
    parser = OfficeConvertingParser(None, registry.resolve)
    with pytest.raises(ParseError, match=r"cannot read \.ppt .*LibreOffice.* Save it as \.pptx"):
        parse(parser, "deck.ppt", b"legacy")
    with pytest.raises(ParseError, match=r"Save it as \.xlsx"):
        parse(parser, "sheet.ods", b"legacy")


def test_legacy_formats_are_recognised(registry):
    assert sniff_extension(fixtures.make_ole("PowerPoint Document")) == "ppt"
    assert sniff_extension(fixtures.make_ole("WordDocument")) == "doc"
    assert sniff_extension(fixtures.make_ole("Workbook")) == "xls"
    assert sniff_extension(b"{\\rtf1\\ansi Refunds}") == "rtf"
    assert sniff_extension(fixtures.make_odt()) == "odt"
    for name, data, media_type in [
        ("upload.bin", fixtures.make_ole("PowerPoint Document"), None),
        ("blob", b"", "application/vnd.ms-powerpoint"),
        ("notes.rtf", b"", None),
        ("plan.odp", b"", None),
        ("old.xls", b"", None),
    ]:
        assert registry.resolve(source(name, data, media_type=media_type)).name == "libreoffice", name
    # the converter is told the sniffed format, not the useless extension
    converter = FakeConverter(b"")
    parser = OfficeConvertingParser(converter, registry.resolve)  # type: ignore[arg-type]
    with pytest.raises(ParseError, match="cannot open pptx"):  # the empty "pptx" it hands back
        parse(parser, "upload.bin", fixtures.make_ole("PowerPoint Document"))
    assert converter.calls == [("ppt", "pptx")]


@needs_libreoffice
def test_libreoffice_round_trip(registry, files):
    converter = OfficeConverter.find()
    assert converter is not None
    ppt = converter.convert(files["review.pptx"], "pptx", "ppt")
    assert sniff_extension(ppt) == "ppt"
    doc = parse(registry.resolve(source("review.ppt", b"")), "review.ppt", ppt)
    assert doc.parser_name == "libreoffice+pptx"
    headings = [e.text for e in doc.elements if e.kind is ElementKind.HEADING]
    assert "Approval limits" in headings and "Limits by region" in headings
    assert "Mention that the EU limit is lower." in text_of(doc)
    assert any(e.kind is ElementKind.TABLE for e in doc.elements)


# --------------------------------------------------------------------------- settings


def test_fallbacks_follow_the_settings():
    from forge_task_documents.config import DocumentsSettings, OcrSettings, OfficeSettings
    from forge_task_documents.task import DocumentsTaskFactory

    off = DocumentsTaskFactory._registry(
        DocumentsSettings(ocr=OcrSettings(enabled=False), office=OfficeSettings(enabled=False))
    )
    pdf = off.resolve(source("a.pdf", b""))
    office = off.resolve(source("a.ppt", b""))
    assert isinstance(pdf, PdfParser) and pdf.ocr is None
    assert isinstance(office, OfficeConvertingParser) and office.converter is None

    on = DocumentsTaskFactory._registry(
        DocumentsSettings(ocr=OcrSettings(command="no-such-tesseract", dpi=200, max_pages=5))
    )
    pdf = on.resolve(source("a.pdf", b""))
    assert isinstance(pdf, PdfParser) and pdf.ocr_dpi == 200 and pdf.max_ocr_pages == 5
    assert pdf.ocr is None  # not installed under that name
