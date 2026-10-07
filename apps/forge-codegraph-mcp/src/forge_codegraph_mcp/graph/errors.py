"""The failures a caller of the graph can act on.

Their messages are safe to show to the model: they name repositories, ids and
bounds, never credentials or storage internals. The tools turn them into MCP
tool errors; anything else is masked.
"""


class GraphError(Exception):
    """A failure whose message the caller may read and act on."""

    kind = "graph error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.kind}: {detail}")
        self.detail = detail


class InvalidRequest(GraphError):
    """Arguments out of range, malformed ids, or a bad cursor."""

    kind = "invalid request"


class NotFound(GraphError):
    """No such repository, record or source at the generation read."""

    kind = "not found"


class IntegrityFailure(GraphError):
    """Stored data that contradicts itself: a payload that doesn't decode, or
    source bytes that don't match their hash."""

    kind = "integrity failure"


class StaleGeneration(GraphError):
    """A cursor from a generation that is no longer the one read."""

    kind = "stale generation"


class Unavailable(GraphError):
    """A service a call needs didn't answer, such as the embedding provider;
    the call may succeed when retried."""

    kind = "unavailable"
