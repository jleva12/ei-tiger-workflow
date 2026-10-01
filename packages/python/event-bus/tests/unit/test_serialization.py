import enum
import json
import math
import sys
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from event_bus.core.errors import SerializationError
from event_bus.core.serialization import (
    JsonSerializer,
    MsgpackSerializer,
    Serializer,
    encode_default,
    json_dumps,
)


class Color(enum.Enum):
    RED = "red"
    STAMP = date(2024, 1, 2)


class Priority(enum.IntEnum):
    HIGH = 1


class Opaque:
    pass


RICH = {
    "dt": datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
    "naive": datetime(2024, 1, 2, 3, 4, 5, 600000),
    "d": date(2024, 1, 2),
    "t": time(3, 4, 5),
    "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "amount": Decimal("12.50"),
    "color": Color.RED,
    "stamp": Color.STAMP,
    "prio": Priority.HIGH,
    "tags": {"b", "c", "a"},
    "frozen": frozenset({3, 1, 2}),
    "pair": (1, "x"),
}

RICH_DECODED = {
    "dt": "2024-01-02T03:04:05+00:00",
    "naive": "2024-01-02T03:04:05.600000",
    "d": "2024-01-02",
    "t": "03:04:05",
    "id": "12345678-1234-5678-1234-567812345678",
    "amount": "12.50",
    "color": "red",
    "stamp": "2024-01-02",
    "prio": 1,
    "tags": ["a", "b", "c"],
    "frozen": [1, 2, 3],
    "pair": [1, "x"],
}


class TestEncodeDefault:
    def test_unsupported_type_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="Opaque"):
            encode_default(Opaque())

    def test_unorderable_set_becomes_list(self) -> None:
        result = encode_default({1, "a"})
        assert sorted(map(str, result)) == ["1", "a"]

    def test_json_dumps_is_compact_and_rejects_nan(self) -> None:
        assert json_dumps({"a": [1, 2], "b": None}) == '{"a":[1,2],"b":null}'
        with pytest.raises(ValueError):
            json_dumps({"x": math.nan})


class TestJsonSerializer:
    def setup_method(self) -> None:
        self.serializer = JsonSerializer()

    def test_implements_protocol(self) -> None:
        assert isinstance(self.serializer, Serializer)

    def test_roundtrip(self) -> None:
        data = {"key": "value", "number": 42, "nested": {"a": [1, 2, 3]}}
        raw = self.serializer.serialize(data)
        assert isinstance(raw, bytes)
        result = self.serializer.deserialize(raw)
        assert result == data

    def test_compact_separators(self) -> None:
        assert self.serializer.serialize({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'

    def test_content_type(self) -> None:
        assert self.serializer.content_type == "application/json"

    def test_deserialize_invalid(self) -> None:
        with pytest.raises(SerializationError):
            self.serializer.deserialize(b"not json")

    def test_deserialize_invalid_utf8(self) -> None:
        with pytest.raises(SerializationError):
            self.serializer.deserialize(b'{"a": "\xff"}')

    def test_rich_types_use_explicit_rules(self) -> None:
        """Regression: default=str wrote '2024-01-02 03:04:05+00:00', '{1, 2}', '<object ...>'."""
        result = self.serializer.deserialize(self.serializer.serialize(RICH))
        assert result == RICH_DECODED

    def test_datetime_is_iso_8601(self) -> None:
        raw = self.serializer.serialize({"ts": datetime(2024, 1, 1)})
        assert self.serializer.deserialize(raw) == {"ts": "2024-01-01T00:00:00"}

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_nan_and_infinity_rejected(self, value: float) -> None:
        with pytest.raises(SerializationError):
            self.serializer.serialize({"x": value})

    @pytest.mark.parametrize("value", [Opaque(), object(), b"bytes", 1 + 2j])
    def test_unsupported_objects_rejected(self, value: Any) -> None:
        with pytest.raises(SerializationError, match="not serializable"):
            self.serializer.serialize({"x": value})

    def test_circular_reference_rejected(self) -> None:
        data: dict[str, Any] = {}
        data["self"] = data
        with pytest.raises(SerializationError):
            self.serializer.serialize(data)

    @pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b"42", b"null", b"true"])
    def test_deserialize_rejects_non_objects(self, raw: bytes) -> None:
        with pytest.raises(SerializationError, match="dict"):
            self.serializer.deserialize(raw)

    def test_serialize_rejects_non_dict(self) -> None:
        with pytest.raises(SerializationError, match="dict"):
            self.serializer.serialize([1, 2])  # type: ignore[arg-type]

    def test_output_is_valid_strict_json(self) -> None:
        raw = self.serializer.serialize(RICH)

        def reject(constant: str) -> None:
            raise AssertionError(f"non-standard JSON constant {constant}")

        json.loads(raw, parse_constant=reject)


class TestMsgpackSerializer:
    @pytest.fixture(autouse=True)
    def _require_msgpack(self) -> None:
        pytest.importorskip("msgpack")

    def test_roundtrip(self) -> None:
        s = MsgpackSerializer()
        data = {"key": "value", "number": 42, "list": [1, 2, 3]}
        raw = s.serialize(data)
        assert isinstance(raw, bytes)
        result = s.deserialize(raw)
        assert result == data

    def test_implements_protocol(self) -> None:
        assert isinstance(MsgpackSerializer(), Serializer)

    def test_content_type(self) -> None:
        """content_type is 'application/msgpack'."""
        assert MsgpackSerializer.content_type == "application/msgpack"
        assert MsgpackSerializer().content_type == "application/msgpack"

    def test_deserialize_invalid(self) -> None:
        """Raises SerializationError on bad data."""
        s = MsgpackSerializer()
        with pytest.raises(SerializationError):
            s.deserialize(b"\xff\xfe invalid msgpack data !@#")

    def test_int_keys_roundtrip(self) -> None:
        """Regression: int keys published fine but failed to decode (strict_map_key)."""
        s = MsgpackSerializer()
        data = {"counts": {1: "one", 2: "two"}}
        assert s.deserialize(s.serialize(data)) == data

    def test_rich_types_use_same_rules_as_json(self) -> None:
        s = MsgpackSerializer()
        result = s.deserialize(s.serialize(RICH))
        assert result == RICH_DECODED

    def test_unsupported_objects_rejected(self) -> None:
        with pytest.raises(SerializationError):
            MsgpackSerializer().serialize({"x": Opaque()})

    def test_deserialize_rejects_non_dict(self) -> None:
        import msgpack

        s = MsgpackSerializer()
        with pytest.raises(SerializationError, match="dict"):
            s.deserialize(msgpack.packb([1, 2, 3]))

    def test_serialize_rejects_non_dict(self) -> None:
        with pytest.raises(SerializationError, match="dict"):
            MsgpackSerializer().serialize("text")  # type: ignore[arg-type]

    def test_msgpack_imported_once_at_construction(self) -> None:
        s = MsgpackSerializer()
        # Later calls no longer import msgpack, so they work even if the
        # module disappears from sys.modules.
        with patch.dict(sys.modules, {"msgpack": None}):
            assert s.deserialize(s.serialize({"a": 1})) == {"a": 1}


def test_msgpack_import_error_raised_at_construction() -> None:
    """Raises a clear ImportError when msgpack is not installed."""
    with (
        patch.dict(sys.modules, {"msgpack": None}),
        pytest.raises(ImportError, match="msgpack is required"),
    ):
        MsgpackSerializer()
