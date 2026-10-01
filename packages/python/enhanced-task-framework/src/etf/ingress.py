"""Ingress contracts: translate an external delivery into an ETF command.

ETF does not own the host's consume loop (a task queue, a message consumer, an HTTP
server, a scheduler, …). Instead, the host receives a delivery and uses a thin
:class:`Ingress` adapter to produce a :class:`LaunchRequest`
(start/resume a run) or a :class:`ResumeRequest` (a human validation decision arriving
out-of-band). The adapter's only job is mapping + supplying an idempotency key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .model import Actor, JobParameters, ValidationDecision


@dataclass(slots=True)
class LaunchRequest:
    """A request to start (or idempotently resume) a run of ``job_name``.

    ``idempotency_key`` dedupes at-least-once delivery (e.g. ``f"kafka:{topic}:{partition}:{offset}"``
    or a queue's message id). ``parameters.identifying`` define the instance identity;
    ``parameters.non_identifying`` are operational knobs. ``labels`` go on the instance the
    launch creates (see :class:`~etf.model.JobInstance`); a launch that finds its instance
    already there leaves that instance's labels as they were.
    """

    job_name: str
    parameters: JobParameters = field(default_factory=JobParameters)
    idempotency_key: str | None = None
    requested_by: Actor | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    source: str | None = None              # e.g. "queue", "kafka", "http"
    priority: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ResumeRequest:
    """A human-in-the-loop decision arriving from outside (UI/HTTP/Kafka topic).

    Identify the gate either by ``validation_request_id`` (preferred) or by
    ``run_id``. The carried :class:`~etf.model.ValidationDecision` drives the resume
    (approve/reject/retry/override), including any non-identifying parameter overrides.
    """

    decision: ValidationDecision
    validation_request_id: str | None = None
    run_id: str | None = None
    correlation_id: str | None = None
    source: str | None = None


class Ingress(ABC):
    """Adapts a transport-specific message into an ETF command.

    Implementations are intentionally thin. A task-queue adapter maps job args; a Kafka
    adapter maps a record's key/value/headers and derives the idempotency key from the
    topic/partition/offset; an HTTP adapter maps a request body. ``source`` labels the
    origin for audit.
    """

    #: Short label identifying the transport (used as ``LaunchRequest.source``).
    source: str = "generic"

    @abstractmethod
    async def to_launch_request(self, message: object) -> LaunchRequest:
        """Map a raw delivery to a :class:`LaunchRequest`."""

    async def to_resume_request(self, message: object) -> ResumeRequest:
        """Map a raw delivery to a :class:`ResumeRequest` (for HITL decisions).

        Optional: only ingresses that carry validation decisions need to implement it.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support resume requests")


# --------------------------------------------------------------------------- #
# Reference adapters (illustrative — see docs/DESIGN.md §3)
# --------------------------------------------------------------------------- #
class CallableLaunchIngress(Ingress):
    """Wrap a plain mapping function as an ingress — handy for tests and HTTP handlers.

    Example::

        ingress = CallableLaunchIngress(
            lambda body: LaunchRequest(
                job_name=body["job"],
                parameters=JobParameters(identifying=body["key"], non_identifying=body.get("opts", {})),
                idempotency_key=body.get("request_id"),
            ),
            source="http",
        )
    """

    def __init__(self, mapper, source: str = "callable") -> None:  # mapper: (object) -> LaunchRequest
        self._mapper = mapper
        self.source = source

    async def to_launch_request(self, message: object) -> LaunchRequest:
        req = self._mapper(message)
        if req.source is None:
            req.source = self.source
        return req
