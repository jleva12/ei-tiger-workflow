"""Storing embeddings in MongoDB, shared by the tasks that search them: vector
encoding (plain lists or BSON binary float32 vectors) and comparing a wanted
search index definition with the one Atlas reports."""

from __future__ import annotations

from typing import Any


def encode_vector(vec: list[float], *, binary: bool) -> Any:
    if not binary:
        return vec
    from bson.binary import Binary, BinaryVectorDtype

    return Binary.from_vector(vec, BinaryVectorDtype.FLOAT32)


def decode_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [float(v) for v in value]
    as_vector = getattr(value, "as_vector", None)
    if as_vector is not None:
        return [float(v) for v in as_vector().data]
    return None


def is_subset(wanted: Any, actual: Any) -> bool:
    """True if every key/value we set is present in ``actual`` (Atlas may add
    defaults). Lists of field specs are compared order-insensitively."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(k in actual and is_subset(v, actual[k]) for k, v in wanted.items())
    if isinstance(wanted, list):
        if not isinstance(actual, list) or len(wanted) != len(actual):
            return False
        if all(isinstance(x, dict) for x in wanted + actual):
            key = lambda d: (str(d.get("type")), str(d.get("path")), str(d.get("name")))  # noqa: E731
            wanted, actual = sorted(wanted, key=key), sorted(actual, key=key)
        return all(is_subset(w, a) for w, a in zip(wanted, actual, strict=True))
    if isinstance(wanted, bool) or isinstance(actual, bool):
        return wanted is actual
    if isinstance(wanted, (int, float)) and isinstance(actual, (int, float)):
        return float(wanted) == float(actual)
    return bool(wanted == actual)
