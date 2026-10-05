"""Errors of the embedding stack."""

from forge_tasks.errors import TransientError


class EmbeddingError(TransientError):
    """The embedding provider failed transiently (rate limit, overload, network)."""
