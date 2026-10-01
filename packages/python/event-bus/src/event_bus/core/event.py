from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class EventMetadata:
    """
    Represents metadata associated with an event in a system.

    This class is used to encapsulate metadata details for an event, facilitating
    tracking, correlation, and content-based operations. It is designed as an
    immutable, slot-based dataclass to ensure efficient memory usage and thread-safe
    handling of event metadata.

    :ivar event_id: A unique identifier for the event.
    :type event_id: str
    :ivar timestamp: The UNIX timestamp of when the event was created.
    :type timestamp: float
    :ivar source: The origin or source system of the event.
    :type source: str
    :ivar correlation_id: An identifier used for correlating the event with a broader set of events.
    :type correlation_id: str
    :ivar causation_id: The identifier of the event that caused the current event, if applicable.
    :type causation_id: str
    :ivar content_type: The content type of the event, typically a MIME type.
    :type content_type: str
    :ivar retry_count: The number of retries attempted for processing the event.
    :type retry_count: int
    :ivar headers: Additional metadata headers associated with the event.
    :type headers: dict[str, str]
    """

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    content_type: str = "application/json"
    retry_count: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    def __hash__(self) -> int:
        # The generated hash would fail on the ``headers`` dict. Hashing the
        # event_id alone stays consistent with the generated __eq__: equal
        # metadata always has equal event_ids.
        return hash(self.event_id)


@dataclass(frozen=True, slots=True)
class Event:
    """
    Represents a communication event within a system.

    The `Event` class serves as the fundamental unit of communication within an
    event-driven architecture. It consists of a topic, a data payload, and metadata
    that includes additional details about the event. The topic is a dot-separated
    hierarchical name, while the data payload is a JSON-serializable dictionary.
    This design enables clear, structured, and efficient communication across
    different parts of the system.

    :ivar topic: The hierarchical name of the event topic (e.g., "orders.created").
    :ivar data: The payload associated with the event, which must be a JSON-serializable dictionary.
    :ivar metadata: Metadata containing supplemental information about the event, such as event ID,
        timestamp, source, and retry count.
    :type metadata: EventMetadata

    The fundamental unit of communication.

    ``topic`` is a dot-separated hierarchical name, e.g.
    ``"orders.created"`` or ``"payments.refund.completed"``.

    ``data`` is the payload -- any JSON-serializable dict.
    """

    topic: str
    data: dict[str, Any]
    metadata: EventMetadata = field(default_factory=EventMetadata)

    @property
    def event_id(self) -> str:
        return self.metadata.event_id

    def __hash__(self) -> int:
        # Events are identified by their event_id. The generated hash would
        # fail on the ``data`` dict; this one stays consistent with the
        # generated __eq__ because equal events always have equal event_ids.
        return hash(self.metadata.event_id)

    def with_retry(self) -> Event:
        """Return a copy with *retry_count* incremented.

        The copy gets its own ``headers`` dict, so changing the retry's headers
        never changes the original event's. ``data`` is shared (not copied).
        """
        new_meta = replace(
            self.metadata,
            retry_count=self.metadata.retry_count + 1,
            headers=dict(self.metadata.headers),
        )
        return Event(topic=self.topic, data=self.data, metadata=new_meta)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """
    Represents a container for an event, its metadata, and delivery information.

    This class is used to encapsulate an event along with additional metadata
    such as the stream identifier, stream name, and delivery count. It is
    designed to be immutable and uses slots for optimized memory usage.

    :ivar event: The event instance contained within the envelope.
    :type event: Event
    :ivar stream_id: The identifier of the event stream, default is an empty string.
    :type stream_id: str
    :ivar stream_name: The name of the event stream, default is an empty string.
    :type stream_name: str
    :ivar delivery_count: The number of times the event has been delivered,
        default is 0.
    :type delivery_count: int
    """

    event: Event
    stream_id: str = ""
    stream_name: str = ""
    delivery_count: int = 0
