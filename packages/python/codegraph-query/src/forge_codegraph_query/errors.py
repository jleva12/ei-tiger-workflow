"""The failures a caller of the graph can act on.

Their messages are safe to show to people and models: they name
repositories, ids and bounds, never credentials or storage internals. The
MCP tools turn them into tool errors and the admin API into HTTP answers
(:func:`status_of`); anything else is masked.
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


def status_of(error: GraphError) -> int:
    """The HTTP status an API answers a graph failure with."""
    if isinstance(error, InvalidRequest):
        return 400
    if isinstance(error, NotFound):
        return 404
    if isinstance(error, StaleGeneration):
        return 409
    if isinstance(error, Unavailable):
        return 503
    return 500
