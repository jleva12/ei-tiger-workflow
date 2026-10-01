"""Pluggable (de)serialization for execution context and parameters.

The checkpoint bag (:class:`etf.context.ExecutionContext`) may hold arbitrary
resume state. ``Serializer`` lets integrators control how that state is encoded for
storage (JSON by default; swap for msgpack, a signed/encrypted codec, etc.).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod


class Serializer(ABC):
    """Encodes execution-context values to/from a storable representation."""

    @abstractmethod
    def serialize(self, value: object) -> object:
        """Return a storage-friendly representation of ``value``."""

    @abstractmethod
    def deserialize(self, raw: object) -> object:
        """Reconstruct a value from its stored representation."""


class JsonSerializer(Serializer):
    """Default serializer: round-trips through JSON-compatible structures.

    Values must be JSON-serializable (or made so via ``default=str``). This keeps stored
    checkpoints human-readable and queryable in MongoDB. For binary/opaque payloads,
    provide a custom serializer.
    """

    def serialize(self, value: object) -> object:
        # Normalize to JSON-compatible structures so the document store can index them.
        # Reject unsupported values instead of silently changing their type with
        # ``default=str``; a checkpoint must resume with the same semantics it saved.
        return json.loads(json.dumps(value, allow_nan=False))

    def deserialize(self, raw: object) -> object:
        return raw
