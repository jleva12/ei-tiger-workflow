from __future__ import annotations

import hashlib
import io
from functools import cached_property
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceFile(BaseModel):
    """Raw bytes of an uploaded file plus the identity it will be indexed under."""

    model_config = ConfigDict(arbitrary_types_allowed=True, ignored_types=(cached_property,))

    tenant_id: str
    doc_id: str
    filename: str
    data: bytes = Field(repr=False)
    media_type: str | None = None
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def extension(self) -> str:
        return PurePosixPath(self.filename).suffix.lower().lstrip(".")

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def open(self) -> io.BytesIO:
        return io.BytesIO(self.data)
