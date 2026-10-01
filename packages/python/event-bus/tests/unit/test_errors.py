"""Tests for event_bus/core/errors.py — exception hierarchy."""

import builtins

import event_bus
from event_bus.core import errors
from event_bus.core.errors import (
    BackpressureError,
    CircuitOpenError,
    ConsumerError,
    DuplicateEventError,
    EventBusConnectionError,
    EventBusError,
    InvalidTopicError,
    PublishError,
    SerializationError,
)


class TestErrorHierarchy:
    def test_all_errors_inherit_from_base(self) -> None:
        """Every error is a subclass of EventBusError."""
        error_classes = [
            EventBusConnectionError,
            InvalidTopicError,
            PublishError,
            SerializationError,
            ConsumerError,
            CircuitOpenError,
            BackpressureError,
            DuplicateEventError,
        ]
        for cls in error_classes:
            assert issubclass(cls, EventBusError), f"{cls.__name__} is not a subclass"

    def test_error_messages(self) -> None:
        """Errors carry the message passed to them."""
        err = EventBusError("test message")
        assert str(err) == "test message"

    def test_connection_error(self) -> None:
        """EventBusConnectionError is an EventBusError."""
        err = EventBusConnectionError("cannot connect")
        assert isinstance(err, EventBusError)
        assert str(err) == "cannot connect"

    def test_invalid_topic_error_is_a_value_error(self) -> None:
        err = InvalidTopicError("bad topic")
        assert isinstance(err, EventBusError)
        assert isinstance(err, ValueError)


class TestDeprecatedConnectionErrorAlias:
    def test_alias_still_importable(self) -> None:
        from event_bus import ConnectionError as PackageAlias
        from event_bus.core.errors import ConnectionError as ModuleAlias

        assert ModuleAlias is EventBusConnectionError
        assert PackageAlias is EventBusConnectionError
        assert errors.ConnectionError is EventBusConnectionError

    def test_alias_does_not_catch_builtin_connection_errors(self) -> None:
        assert not issubclass(builtins.ConnectionError, EventBusError)
        assert not issubclass(EventBusConnectionError, builtins.ConnectionError)

    def test_star_import_does_not_shadow_builtin(self) -> None:
        assert "ConnectionError" not in event_bus.__all__
        assert "EventBusConnectionError" in event_bus.__all__
        assert "InvalidTopicError" in event_bus.__all__
        namespace: dict[str, object] = {}
        exec("from event_bus import *", namespace)
        assert "ConnectionError" not in namespace
        assert namespace["EventBusConnectionError"] is EventBusConnectionError

    def test_all_exports_resolve(self) -> None:
        for name in event_bus.__all__:
            assert hasattr(event_bus, name), name

    def test_publish_error(self) -> None:
        """PublishError is an EventBusError."""
        err = PublishError("publish failed")
        assert isinstance(err, EventBusError)
        assert str(err) == "publish failed"

    def test_serialization_error(self) -> None:
        """SerializationError is an EventBusError."""
        err = SerializationError("bad data")
        assert isinstance(err, EventBusError)
        assert str(err) == "bad data"

    def test_circuit_open_error(self) -> None:
        """CircuitOpenError is an EventBusError."""
        err = CircuitOpenError("circuit is open")
        assert isinstance(err, EventBusError)
        assert str(err) == "circuit is open"
