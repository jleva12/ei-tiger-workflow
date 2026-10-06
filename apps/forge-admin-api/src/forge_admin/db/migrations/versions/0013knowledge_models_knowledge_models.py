"""knowledge_models: the embedding model each knowledge base document's
chunks are embedded with

A search compares its question's vector only with vectors of the same model,
so a document embedded with another one (before the configured model
changed) is found by its words alone until it's re-indexed.
knowledge_documents.embedding_model is the model the worker reported when
its ingest succeeded; documents from before have none, and count as needing
re-indexing until they are.

Revision ID: 0013knowledge_models
Revises: 0012adk_run_versions
Create Date: 2026-10-05 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013knowledge_models"
down_revision: str | Sequence[str] | None = "0012adk_run_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "embedding_model", sa.String(length=255), server_default="", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_documents", "embedding_model")
