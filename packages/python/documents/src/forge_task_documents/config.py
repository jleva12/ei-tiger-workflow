"""The documents task's options: ``HYBRID_DOCUMENTS__*``, and where s3:// source
files are read from, ``HYBRID_S3__*``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, SecretStr, field_validator

from forge_task_documents.chunking.types import ChunkingConfig
from forge_task_documents.ingestion.pipeline import PipelineConfig
from forge_task_documents.retrieval.service import SearchConfig


class ContextSettings(BaseModel):
    """LLM contextual retrieval for chunks (1 LLM call per chunk)."""

    enabled: bool = False
    concurrency: int = 8


class OcrSettings(BaseModel):
    """OCR of PDF pages with no text layer (scans), with Tesseract when it is installed."""

    enabled: bool = True
    command: str = "tesseract"
    languages: str = "eng"  # tesseract -l: "eng+deu", with a tesseract-ocr-<lang> pack per language
    dpi: int = 300
    max_pages: int = 200  # per document; later scanned pages are left unread
    page_timeout_s: float = 120.0
    min_confidence: float = 30.0


class OfficeSettings(BaseModel):
    """Legacy (.ppt, .doc, .xls), RTF and OpenDocument files, converted with
    LibreOffice when it is installed."""

    enabled: bool = True
    command: str = "soffice"
    timeout_s: float = 180.0


class DocumentsSettings(BaseModel):
    storage_backend: str = "mongo"  # "mongo" | "memory" | anything registered
    documents_collection: str = "documents"
    chunks_collection: str = "chunks"
    text_index: str = "chunks_text"
    vector_index: str = "chunks_vector"
    vector_similarity: Literal["dotProduct", "cosine", "euclidean"] = "dotProduct"
    vector_quantization: Literal["none", "scalar", "binary"] = "scalar"
    language_analyzer: str = "lucene.english"
    binary_vectors: bool = True
    fusion_mode: Literal["server", "client"] = "server"
    docx_parser: Literal["python-docx", "docling"] = "python-docx"
    # docling OCRs scanned pages and models complex layouts; pdfplumber reads the text layer.
    pdf_parser: Literal["pdfplumber", "docling"] = "pdfplumber"
    ocr: OcrSettings = OcrSettings()
    office: OfficeSettings = OfficeSettings()
    max_file_bytes: int = 100 * 1024 * 1024
    embedding_profile: str = "default"
    context: ContextSettings = ContextSettings()
    chunking: ChunkingConfig = ChunkingConfig()
    search: SearchConfig = SearchConfig()
    pipeline: PipelineConfig = PipelineConfig()


class S3Settings(BaseModel):
    """Where s3:// source files are read from. Unset fields fall back to
    boto3's own configuration (AWS_* variables, instance roles); an
    endpoint_url means an S3-compatible server, such as the local RustFS,
    addressed path-style."""

    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: SecretStr | None = None

    @field_validator("endpoint_url", "region", "access_key_id", "secret_access_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value
