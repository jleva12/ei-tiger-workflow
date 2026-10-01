"""Tests for event_bus/config.py — configuration dataclasses."""

import pytest

from event_bus.config import EventBusConfig, RedisConfig, redact_url


class TestRedisConfig:
    def test_defaults(self) -> None:
        """All defaults match expected values."""
        cfg = RedisConfig()
        assert cfg.url == "redis://localhost:6379/0"
        assert cfg.max_connections == 20
        assert cfg.max_blocking_connections == 10
        assert cfg.max_pubsub_connections == 10
        assert cfg.key_prefix == "eventbus"
        assert cfg.max_stream_length == 100_000
        assert cfg.health_check_interval == 10.0
        assert cfg.socket_timeout == 5.0
        assert cfg.socket_connect_timeout == 5.0

    def test_custom(self) -> None:
        """Custom values override defaults."""
        cfg = RedisConfig(url="redis://other:6380/1", max_connections=50, key_prefix="myapp")
        assert cfg.url == "redis://other:6380/1"
        assert cfg.max_connections == 50
        assert cfg.key_prefix == "myapp"

    def test_circuit_breaker_defaults(self) -> None:
        """Circuit breaker fields have correct defaults."""
        cfg = RedisConfig()
        assert cfg.circuit_breaker_failure_threshold == 5
        assert cfg.circuit_breaker_recovery_timeout == 30.0
        assert cfg.circuit_breaker_half_open_max_calls == 3


class TestEventBusConfig:
    def test_defaults(self) -> None:
        """All defaults match expected values."""
        cfg = EventBusConfig()
        assert cfg.serializer == "json"
        assert cfg.max_pending_publishes == 1000
        assert cfg.shutdown_timeout == 30.0
        assert cfg.enable_idempotency is False
        assert cfg.idempotency_ttl == 86400

    def test_custom(self) -> None:
        """Custom values override defaults."""
        cfg = EventBusConfig(
            max_pending_publishes=500,
            shutdown_timeout=10.0,
            enable_idempotency=True,
            idempotency_ttl=3600,
        )
        assert cfg.max_pending_publishes == 500
        assert cfg.shutdown_timeout == 10.0
        assert cfg.enable_idempotency is True
        assert cfg.idempotency_ttl == 3600

    def test_nested_redis_is_deprecated(self) -> None:
        """EventBusConfig.redis is ignored by the bus: setting it warns."""
        redis_cfg = RedisConfig(url="redis://custom:6379/2")
        with pytest.warns(DeprecationWarning, match="RedisTransport"):
            cfg = EventBusConfig(redis=redis_cfg)
        assert cfg.redis.url == "redis://custom:6379/2"

    def test_serializer_setting_is_deprecated(self) -> None:
        with pytest.warns(DeprecationWarning, match="RedisConfig.serializer"):
            EventBusConfig(serializer="msgpack")

    def test_metric_topic_depth(self) -> None:
        assert EventBusConfig().metric_topic("orders.123.created") == "orders.123.created"
        assert EventBusConfig(metrics_topic_depth=1).metric_topic("orders.123.created") == "orders"
        with pytest.raises(ValueError):
            EventBusConfig(metrics_topic_depth=0)


class TestRedisConfigSafety:
    def test_url_password_is_redacted(self) -> None:
        cfg = RedisConfig(url="redis://app:s3cret@redis.internal:6380/1")
        assert cfg.safe_url == "redis://app:***@redis.internal:6380/1"
        assert redact_url("redis://:s3cret@h:6379/0") == "redis://:***@h:6379/0"
        assert redact_url("redis://h:6379/0") == "redis://h:6379/0"
        assert "s3cret" not in repr(cfg)

    def test_unknown_serializer_is_refused(self) -> None:
        with pytest.raises(ValueError):
            RedisConfig(serializer="xml")
