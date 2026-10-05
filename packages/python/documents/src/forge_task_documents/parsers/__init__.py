from forge_task_documents.parsers.csv import CsvParser
from forge_task_documents.parsers.docx import DocxParser
from forge_task_documents.parsers.markdown import MarkdownParser
from forge_task_documents.parsers.mermaid import MermaidParser
from forge_task_documents.parsers.ocr import OcrEngine, TesseractOcr
from forge_task_documents.parsers.office import OfficeConverter, OfficeConvertingParser
from forge_task_documents.parsers.pdf import PdfParser
from forge_task_documents.parsers.pptx import PptxParser
from forge_task_documents.parsers.registry import ParserRegistry, sniff_extension
from forge_task_documents.parsers.text import TextParser
from forge_task_documents.parsers.visio import VisioParser
from forge_task_documents.parsers.xlsx import XlsxParser


def default_registry(*, load_plugins: bool = True) -> ParserRegistry:
    """Every built-in parser. The fallbacks that need system packages are on
    when those are installed: OCR of scanned PDF pages (Tesseract) and legacy
    or OpenDocument office files (LibreOffice)."""
    registry = ParserRegistry(
        [
            PdfParser(ocr=TesseractOcr.find()),
            DocxParser(),
            PptxParser(),
            XlsxParser(),
            CsvParser(),
            MarkdownParser(),
            MermaidParser(),
            TextParser(),
            VisioParser(),
        ]
    )
    registry.register(OfficeConvertingParser(OfficeConverter.find(), registry.resolve))
    if load_plugins:
        registry.load_entry_points()
    return registry


__all__ = [
    "CsvParser",
    "DocxParser",
    "MarkdownParser",
    "MermaidParser",
    "OcrEngine",
    "OfficeConverter",
    "OfficeConvertingParser",
    "ParserRegistry",
    "PdfParser",
    "PptxParser",
    "TesseractOcr",
    "TextParser",
    "VisioParser",
    "XlsxParser",
    "default_registry",
    "sniff_extension",
]
