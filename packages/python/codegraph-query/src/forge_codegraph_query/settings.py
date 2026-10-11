"""How every app reads the code graph: one settings model, nested under each
app's own prefix, so the admin API, the workflows worker and the code graph
MCP server can't drift apart on the database, the embedding model or the
search options.

    FORGE_ADMIN_CODEGRAPH__SPANNER__DATABASE        the admin API
    HYBRID_ADK_WORKFLOWS__CODEGRAPH__SPANNER__...   ADK workflows
    CODEGRAPH_SPANNER__DATABASE                     the MCP server (its own groups)

Each app's ``.env`` takes the values from the repository's ``.env.common``:
``FORGE_CODEGRAPH_SPANNER_DATABASE``, ``FORGE_CODEGRAPH_CURSOR_SIGNING_KEY``,
``FORGE_CODEGRAPH_SEARCH_ENHANCE_QUERY`` and ``FORGE_EMBEDDING_*``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator

from forge_codegraph_query.embedding import DEFAULT_DIMENSIONS, DEFAULT_MAX_INPUT_BYTES

DATABASE = re.compile(r"^projects/[^/\s]+/instances/[^/\s]+/databases/[^/\s]+$")
# The worker's database on the local Spanner emulator (make worker).
LOCAL_DATABASE = "projects/codegraph-local/instances/codegraph/databases/codegraph"


def _blank_is_unset(value: Any) -> Any:
    """``.env`` templates leave values empty: unset, not empty strings."""
    return None if isinstance(value, str) and not value.strip() else value


class SpannerSettings(BaseModel):
    """The code graph worker's database: the graph it writes is read here.

    The Spanner client takes the emulator from the process environment
    (``SPANNER_EMULATOR_HOST``); an empty value means managed Spanner."""

    # projects/<p>/instances/<i>/databases/<d>; unset, the code graph is off.
    database: str | None = None
    # The deployment scope cursors are fingerprinted with.
    scope: str = Field(default="codegraph", min_length=1, max_length=128)
    # Signs pagination cursors; at least 32 characters.
    cursor_signing_key: SecretStr | None = Field(default=None, min_length=32)
    # A service account's JSON key, for hosts without key files; unset uses
    # Application Default Credentials (or the emulator).
    credentials_json: SecretStr | None = None
    # Bound on each query, in seconds.
    timeout: float = Field(default=30.0, gt=0, le=120)
    # How long an app waits for the database at startup: the worker creates
    # it (and the emulator's schema) when it starts.
    startup_timeout: float = Field(default=60.0, ge=0)

    _blanks = field_validator("database", "cursor_signing_key", "credentials_json", mode="before")(
        _blank_is_unset
    )

    @field_validator("database")
    @classmethod
    def _database(cls, value: str | None) -> str | None:
        if value is not None and not DATABASE.match(value):
            raise ValueError("spanner.database must be projects/<p>/instances/<i>/databases/<d>")
        return value


class EmbeddingSettings(BaseModel):
    """The worker's embedding model. With a model and an API key, searches add
    the semantic branch: questions are embedded as the worker embedded the
    graph, so the model, dimensions and base URL must be the worker's
    (``FORGE_EMBEDDING_*`` in ``.env.common``)."""

    api_key: SecretStr | None = None
    # An OpenAI-compatible gateway; empty for OpenAI.
    base_url: str | None = None
    model: str = ""
    # The database's vector length; 0 means the model's default, 3072.
    dimensions: int = Field(default=0, ge=0, le=4096)
    max_input_bytes: int = Field(default=DEFAULT_MAX_INPUT_BYTES, ge=256, le=8000)

    _blanks = field_validator("api_key", "base_url", mode="before")(_blank_is_unset)

    @property
    def enabled(self) -> bool:
        return bool(self.model and self.api_key and self.api_key.get_secret_value())

    @property
    def vector_length(self) -> int:
        return self.dimensions or DEFAULT_DIMENSIONS


class SearchSettings(BaseModel):
    # Spanner's query enhancement (spelling, synonyms, plurals, IDF scoring)
    # on managed Spanner; the emulator ignores it.
    enhance_query: bool = False


class CodeGraphSettings(BaseModel):
    spanner: SpannerSettings = SpannerSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    search: SearchSettings = SearchSettings()

    @property
    def enabled(self) -> bool:
        """Whether the code graph is set up: a database and a cursor key."""
        return self.spanner.database is not None and self.spanner.cursor_signing_key is not None

    @property
    def missing(self) -> str:
        """What's left to set, for the message of a code graph that's off."""
        names = []
        if self.spanner.database is None:
            names.append("spanner.database")
        if self.spanner.cursor_signing_key is None:
            names.append("spanner.cursor_signing_key")
        return ", ".join(names)
