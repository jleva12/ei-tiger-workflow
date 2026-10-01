from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


def redact_url(url: str) -> str:
    """The URL with any password replaced by ``***``, safe to log."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if parts.password is None:
        return url
    user = parts.username or ""
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"{user}:***@{host}" if user else f":***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass
class RedisConfig:
    """Redis connection configuration."""

    # Kept out of repr (it may hold a password); logs use ``redact_url``.
    url: str = field(default="redis://localhost:6379/0", repr=False)
    # Pool sizes. Pools block (up to ``pool_timeout``) when every connection is
    # in use rather than failing: a burst queues instead of erroring.
    max_connections: int = 20
    max_blocking_connections: int = 10
    max_pubsub_connections: int = 10
    pool_timeout: float = 10.0
    key_prefix: str = "eventbus"
    # A safety cap on each stream's length (approximate trimming at publish).
    # Consumer groups also trim what every group has consumed (lag-aware), so
    # streams normally stay far below it; entries past the cap are dropped even
    # if unprocessed, so keep it well above any expected consumer lag. None
    # disables the cap.
    max_stream_length: int | None = 100_000
    # Each consumer group's dead-letter stream is capped at this many entries
    # (approximately; oldest dropped, with a warning). None disables the cap.
    dlq_max_length: int | None = 50_000
    health_check_interval: float = 10.0
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    # Blocking reads (XREADGROUP BLOCK) and pub/sub connections need a timeout
    # too, or a silent network partition hangs them forever. Keep the blocking
    # one above every consumer group's ``block_ms``.
    blocking_socket_timeout: float = 30.0
    pubsub_socket_timeout: float = 30.0
    # "json" or "msgpack", used when no serializer is passed to the transport.
    serializer: str = "json"

    # Circuit breaker
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 30.0
    circuit_breaker_half_open_max_calls: int = 3

    def __post_init__(self) -> None:
        if self.serializer not in ("json", "msgpack"):
            raise ValueError(f"serializer must be 'json' or 'msgpack', not {self.serializer!r}")
        if self.pool_timeout <= 0:
            raise ValueError("pool_timeout must be positive")

    @property
    def safe_url(self) -> str:
        """The URL with any password redacted."""
        return redact_url(self.url)


_DEFAULT_REDIS = RedisConfig()


@dataclass
class EventBusConfig:
    """Top-level event bus configuration."""

    # Deprecated and ignored: the transport is configured by the RedisConfig
    # passed to RedisTransport (which also picks the serializer). Setting
    # either to a non-default value warns.
    redis: RedisConfig = field(default_factory=RedisConfig)
    serializer: str = "json"
    max_pending_publishes: int = 1000
    shutdown_timeout: float = 30.0
    # Skip events a consumer group has already processed (by event ID), and
    # hold a lease while one is processed, so a republished duplicate isn't
    # handled twice. Keys live ``idempotency_ttl`` seconds after success.
    enable_idempotency: bool = False
    idempotency_ttl: int = 86400  # 24 hours
    idempotency_lease: int = 300
    # How long start() waits for the pub/sub subscription to be confirmed.
    subscribe_timeout: float = 5.0
    # Tag metrics with the first N topic segments instead of the whole topic
    # (bounded cardinality when topics carry IDs). None keeps the full topic.
    metrics_topic_depth: int | None = None

    def __post_init__(self) -> None:
        if self.redis != _DEFAULT_REDIS:
            warnings.warn(
                "EventBusConfig.redis is ignored: pass the RedisConfig to RedisTransport",
                DeprecationWarning,
                stacklevel=3,
            )
        if self.serializer != "json":
            warnings.warn(
                "EventBusConfig.serializer is ignored: set RedisConfig.serializer "
                "(or pass a serializer to RedisTransport)",
                DeprecationWarning,
                stacklevel=3,
            )
        if self.metrics_topic_depth is not None and self.metrics_topic_depth < 1:
            raise ValueError("metrics_topic_depth must be at least 1")

    def metric_topic(self, topic: str) -> str:
        """The topic as metrics tag it."""
        if self.metrics_topic_depth is None:
            return topic
        return ".".join(topic.split(".")[: self.metrics_topic_depth])
