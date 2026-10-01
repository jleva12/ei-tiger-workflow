from __future__ import annotations

import datetime as dt
import enum
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from event_bus.core.errors import SerializationError

# Values the encoders support natively; encode_default() is never asked for these.
_NATIVE = (str, int, float, bool, type(None), list, tuple, dict)


def encode_default(obj: Any) -> Any:
    """Convert a value the encoders do not support natively.

    Used as ``default=`` by :func:`json_dumps` and :class:`MsgpackSerializer`,
    so every payload is encoded by the same explicit rules:

    - ``datetime``, ``date``, ``time`` -> ISO 8601 string (``isoformat()``)
    - ``UUID``, ``Decimal`` -> ``str``
    - ``Enum`` -> its ``.value`` (converted again if needed)
    - ``set``, ``frozenset`` -> list, sorted when the items are comparable
    - ``tuple`` -> list

    :raises TypeError: for any other type, instead of silently writing its
        ``str()`` (``'<object at 0x...>'``) into the payload.
    """
    if isinstance(obj, dt.date | dt.time):  # datetime is a subclass of date
        return obj.isoformat()
    if isinstance(obj, uuid.UUID | Decimal):
        return str(obj)
    if isinstance(obj, enum.Enum):
        value = obj.value
        return value if isinstance(value, _NATIVE) else encode_default(value)
    if isinstance(obj, set | frozenset):
        try:
            return sorted(obj)
        except TypeError:  # items of mixed or unorderable types
            return list(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not serializable")


def json_dumps(obj: Any) -> str:
    """Encode *obj* as compact JSON using the event bus encoding rules.

    ``NaN`` and infinities are rejected (they are not valid JSON and browsers
    cannot parse them) and unknown types go through :func:`encode_default`.

    :raises TypeError: for values of unsupported types.
    :raises ValueError: for ``NaN``/infinite floats or circular references.
    """
    return json.dumps(obj, separators=(",", ":"), default=encode_default, allow_nan=False)


def _require_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SerializationError(f"{what} must be a JSON object (dict), not {type(value).__name__}")
    return value


@runtime_checkable
class Serializer(Protocol):
    """Pluggable serialization strategy."""

    content_type: str

    def serialize(self, data: dict[str, Any]) -> bytes: ...

    def deserialize(self, raw: bytes) -> dict[str, Any]: ...


class JsonSerializer:
    """Compact UTF-8 JSON, encoded with :func:`json_dumps`."""

    content_type: str = "application/json"

    def serialize(self, data: dict[str, Any]) -> bytes:
        _require_dict(data, "serialized data")
        try:
            return json_dumps(data).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise SerializationError(f"JSON serialization failed: {exc}") from exc

    def deserialize(self, raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as exc:  # JSONDecodeError, UnicodeDecodeError
            raise SerializationError(f"JSON deserialization failed: {exc}") from exc
        return _require_dict(data, "deserialized JSON")


class MsgpackSerializer:
    """msgpack encoding (optional ``event-bus[msgpack]`` extra).

    Uses the same :func:`encode_default` rules as the JSON serializer for types
    msgpack does not support natively, and unpacks with ``strict_map_key=False``
    so maps with non-string keys (e.g. ``{1: "a"}``) round-trip.
    """

    content_type: str = "application/msgpack"

    def __init__(self) -> None:
        try:
            import msgpack  # optional dependency
        except ImportError as exc:
            raise ImportError(
                "msgpack is required for MsgpackSerializer. "
                "Install it with: pip install event-bus[msgpack]"
            ) from exc
        self._packb: Callable[..., Any] = msgpack.packb
        self._unpackb: Callable[..., Any] = msgpack.unpackb

    def serialize(self, data: dict[str, Any]) -> bytes:
        _require_dict(data, "serialized data")
        try:
            packed: bytes = self._packb(data, use_bin_type=True, default=encode_default)
        except Exception as exc:
            raise SerializationError(f"msgpack serialization failed: {exc}") from exc
        return packed

    def deserialize(self, raw: bytes) -> dict[str, Any]:
        try:
            data = self._unpackb(raw, raw=False, strict_map_key=False)
        except Exception as exc:
            raise SerializationError(f"msgpack deserialization failed: {exc}") from exc
        return _require_dict(data, "deserialized msgpack")
