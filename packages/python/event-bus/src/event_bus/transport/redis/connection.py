from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from typing import cast

import redis.asyncio as aioredis
import redis.exceptions as redis_errors

from event_bus.config import RedisConfig
from event_bus.core.errors import EventBusConnectionError
from event_bus.resilience.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


def is_connectivity_error(exc: BaseException) -> bool:
    """
    Whether an error says Redis itself is unreachable or unresponsive, which
    is what the circuit breaker counts. A busy local connection pool, a bad
    payload or a command error doesn't.
    """
    if isinstance(exc, redis_errors.MaxConnectionsError):
        return False
    if isinstance(exc, redis_errors.ConnectionError) and "No connection available" in str(exc):
        return False  # the blocking pool timed out waiting for a free connection
    return isinstance(
        exc, (redis_errors.ConnectionError, redis_errors.TimeoutError, OSError, TimeoutError)
    )


class RedisConnectionManager:
    """
    Manages three connection pools:

    1. ``command_pool`` -- for normal commands (XADD, XACK, etc.)
    2. ``blocking_pool`` -- for blocking reads (XREADGROUP with block > 0)
    3. ``pubsub_pool`` -- for long-lived Pub/Sub subscriptions

    Every pool blocks for up to ``pool_timeout`` when all its connections are
    in use, so a burst waits for a connection instead of failing. Every
    connection has a socket timeout, so a silent network partition surfaces
    as an error instead of a hang.

    Also owns the circuit breaker (settled by real operations only) and runs
    periodic health checks.
    """

    def __init__(self, config: RedisConfig) -> None:
        self._config = config
        self._command_client: aioredis.Redis | None = None
        self._blocking_client: aioredis.Redis | None = None
        self._pubsub_client: aioredis.Redis | None = None
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=config.circuit_breaker_failure_threshold,
            recovery_timeout=config.circuit_breaker_recovery_timeout,
            half_open_max_calls=config.circuit_breaker_half_open_max_calls,
        )
        self._health_check_task: asyncio.Task[None] | None = None
        self._healthy = False
        logger.debug(
            "RedisConnectionManager created "
            "(max_connections=%d, max_blocking=%d, max_pubsub=%d, "
            "health_check_interval=%.1fs)",
            config.max_connections,
            config.max_blocking_connections,
            config.max_pubsub_connections,
            config.health_check_interval,
        )

    def _pool(
        self, max_connections: int, socket_timeout: float, **extra: object
    ) -> aioredis.BlockingConnectionPool:
        return aioredis.BlockingConnectionPool.from_url(
            self._config.url,
            max_connections=max_connections,
            timeout=self._config.pool_timeout,
            socket_timeout=socket_timeout,
            socket_connect_timeout=self._config.socket_connect_timeout,
            decode_responses=False,
            **extra,
        )

    async def connect(self) -> None:
        logger.debug("Creating connection pools for %s", self._config.safe_url)
        command_pool = self._pool(self._config.max_connections, self._config.socket_timeout)
        blocking_pool = self._pool(
            self._config.max_blocking_connections, self._config.blocking_socket_timeout
        )
        # Pub/sub connections sit idle between messages: a periodic PING
        # (health_check_interval) notices a dead one, and the socket timeout
        # bounds the wait for its reply.
        pubsub_pool = self._pool(
            self._config.max_pubsub_connections,
            self._config.pubsub_socket_timeout,
            health_check_interval=max(1, int(self._config.health_check_interval)),
        )
        self._command_client = aioredis.Redis.from_pool(command_pool)
        self._blocking_client = aioredis.Redis.from_pool(blocking_pool)
        self._pubsub_client = aioredis.Redis.from_pool(pubsub_pool)

        logger.debug("Pinging Redis to verify connectivity...")
        try:
            await cast(Awaitable[bool], self._command_client.ping())
        except Exception as exc:
            # Ping failed -- tear down everything so we don't leak pools
            logger.error("Initial Redis ping failed, closing pools", exc_info=True)
            await self.disconnect()
            raise EventBusConnectionError(
                f"Can't reach Redis at {self._config.safe_url}: {exc}"
            ) from exc

        self._healthy = True
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(), name="eventbus-redis-health"
        )
        logger.debug("Connection manager ready, health check loop started")

    async def disconnect(self) -> None:
        logger.debug("Disconnecting RedisConnectionManager")
        if self._health_check_task:
            self._health_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_check_task
            self._health_check_task = None
            logger.debug("Health check loop cancelled")
        for name in ("_command_client", "_blocking_client", "_pubsub_client"):
            client: aioredis.Redis | None = getattr(self, name)
            if client is not None:
                setattr(self, name, None)
                with contextlib.suppress(Exception):
                    await client.aclose()
        self._healthy = False

    def _require(self, client: aioredis.Redis | None) -> aioredis.Redis:
        if client is None:
            raise EventBusConnectionError("The Redis transport is not connected")
        return client

    @property
    def client(self) -> aioredis.Redis:
        """Normal command client (non-blocking operations)."""
        return self._require(self._command_client)

    @property
    def blocking_client(self) -> aioredis.Redis:
        """Client for blocking operations (XREADGROUP with block)."""
        return self._require(self._blocking_client)

    @property
    def pubsub_client(self) -> aioredis.Redis:
        """Client for long-lived Pub/Sub subscriptions."""
        return self._require(self._pubsub_client)

    @property
    def connected(self) -> bool:
        return self._command_client is not None

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def is_healthy(self) -> bool:
        return self._healthy and self._circuit_breaker.state != "open"

    async def _ping(self) -> bool:
        # The health check only reports: it never opens or closes the circuit
        # breaker (PING can succeed while writes fail, e.g. OOM or READONLY).
        try:
            if self._command_client is None:
                logger.debug("Ping skipped: no command client")
                return False
            await cast(Awaitable[bool], self._command_client.ping())
            self._healthy = True
            logger.debug("Redis ping succeeded")
            return True
        except Exception as exc:
            self._healthy = False
            logger.warning("Redis health check failed: %s", exc)
            return False

    async def _health_check_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.health_check_interval)
            await self._ping()
