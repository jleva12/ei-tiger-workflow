"""Audit event model and the pluggable ``AuditSink`` contract.

Every meaningful transition emits an immutable :class:`AuditEvent`. Sinks decide where
those go: the default :class:`StoreBackedAuditSink` writes them to the state store; a
:class:`CompositeAuditSink` fans out to several destinations (store + Kafka + OTel, …).
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import Actor, FailureRecord
from .status import AuditEventType, BatchStatus, ExitStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class AuditEvent:
    """An immutable, append-only record of something that happened to a run.

    Ordered within a run by :attr:`sequence`. Carries the actor (who), the status
    transition, an optional structured :class:`~etf.model.FailureRecord`, and
    correlation/trace ids for stitching into distributed traces.
    """

    id: str
    event_type: AuditEventType
    run_id: str
    instance_id: str
    sequence: int
    at: datetime = field(default_factory=_utcnow)
    actor: Actor | None = None
    step_name: str | None = None
    from_status: BatchStatus | None = None
    to_status: BatchStatus | None = None
    exit_status: ExitStatus | None = None
    failure: FailureRecord | None = None
    message: str = ""
    attributes: dict[str, object] = field(default_factory=dict)
    correlation_id: str | None = None
    trace_id: str | None = None


@dataclass(slots=True)
class AuditQuery:
    """Filter for :meth:`AuditSink.query`."""

    run_id: str | None = None
    instance_id: str | None = None
    event_types: Sequence[AuditEventType] | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 200
    offset: int = 0


class AuditSink(ABC):
    """Receives, persists, and (optionally) serves the audit event stream."""

    @abstractmethod
    async def emit(self, event: AuditEvent) -> None:
        """Durably record a single event."""

    async def emit_batch(self, events: Sequence[AuditEvent]) -> None:
        """Record several events. Default: sequential ``emit``; override for batching."""
        for event in events:
            await self.emit(event)

    @abstractmethod
    async def query(self, query: AuditQuery) -> list[AuditEvent]:
        """Return events matching ``query``, ordered by ``(run_id, sequence)``.

        Sinks that only forward (e.g. a fire-and-forget Kafka sink) may return ``[]``
        or raise ``NotImplementedError``; querying is then served by another sink.
        """


class CompositeAuditSink(AuditSink):
    """Fans an event out to several sinks. The first sink serves queries."""

    def __init__(self, *sinks: AuditSink) -> None:
        if not sinks:
            raise ValueError("CompositeAuditSink requires at least one sink")
        self._sinks = sinks

    async def emit(self, event: AuditEvent) -> None:
        first, *external = self._sinks
        await first.emit(event)
        store = getattr(first, "_store", None)
        for sink in external:
            event_copy = copy.deepcopy(event)

            async def deliver(sink: AuditSink = sink, item: AuditEvent = event_copy) -> None:
                await sink.emit(item)

            if store is not None and store.defer_after_commit(deliver):
                continue
            await deliver()

    async def query(self, query: AuditQuery) -> list[AuditEvent]:
        return await self._sinks[0].query(query)


class LoggingAuditSink(AuditSink):
    """Write-only sink that logs events via the stdlib logger (no query support)."""

    def __init__(self, logger_name: str = "etf.audit") -> None:
        import logging

        self._log = logging.getLogger(logger_name)

    async def emit(self, event: AuditEvent) -> None:
        self._log.info(
            "audit %s run=%s step=%s %s->%s actor=%s %s",
            event.event_type.value,
            event.run_id,
            event.step_name,
            event.from_status,
            event.to_status,
            event.actor.id if event.actor else "-",
            event.message,
        )

    async def query(self, query: AuditQuery) -> list[AuditEvent]:
        raise NotImplementedError("LoggingAuditSink does not support queries")


class StoreBackedAuditSink(AuditSink):
    """Default sink: persists events through the :class:`~etf.store.StateStore`.

    Delegates to ``store.append_audit`` / ``store.query_audit`` so audit history lives
    alongside run state and is queryable for a full timeline.
    """

    def __init__(self, store: "object") -> None:
        # Typed as object to avoid importing StateStore (keeps import graph acyclic);
        # any StateStore implementation satisfies the duck-typed calls below.
        self._store = store

    async def emit(self, event: AuditEvent) -> None:
        await self._store.append_audit(event)  # type: ignore[attr-defined]

    async def query(self, query: AuditQuery) -> list[AuditEvent]:
        return await self._store.query_audit(query)  # type: ignore[attr-defined]
