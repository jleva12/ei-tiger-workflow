"""Backend selection shared by ingestion, CLI and API retrieval.

The domain protocols (StorageBackend and CommitStorage) are the public seams;
this package supplies the common Spanner persistence and search primitives.
"""

from forge_tasks.env_files import environment


def selected_backend(default: str = "mongo") -> str:
    value = environment().get("FORGE_VECTOR_STORE")
    if value is None:
        return default  # preserves registered task-specific backends
    value = value.strip().lower()
    if value not in {"mongo", "spanner"}:
        raise ValueError("FORGE_VECTOR_STORE must be mongo or spanner")
    return value


def spanner_database() -> str:
    import re

    value = environment().get("FORGE_VECTOR_SPANNER_DATABASE", "")
    if not re.fullmatch(r"projects/[^/]+/instances/[^/]+/databases/[^/]+", value):
        raise ValueError(
            "FORGE_VECTOR_SPANNER_DATABASE must be projects/<project>/instances/<instance>/databases/<database>"
        )
    return value
