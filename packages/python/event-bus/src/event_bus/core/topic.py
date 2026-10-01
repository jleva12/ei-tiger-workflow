from __future__ import annotations

import re
from dataclasses import dataclass, field

from event_bus.core.errors import InvalidTopicError

#: Maximum length, in characters, of a topic or a topic pattern.
MAX_TOPIC_LENGTH = 255
#: Maximum number of dot-separated segments in a topic pattern.
MAX_PATTERN_SEGMENTS = 32

SINGLE_WILDCARD = "*"
MULTI_WILDCARD = "**"
_WILDCARDS = frozenset({SINGLE_WILDCARD, MULTI_WILDCARD})

# One topic segment: letters, digits, "_", "-" and ":". Segments exclude ".",
# so the topic regex below cannot backtrack (each "." is an unambiguous split).
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_\-:]+")
_TOPIC_RE = re.compile(r"[A-Za-z0-9_\-:]+(?:\.[A-Za-z0-9_\-:]+)*")

# Shape of the precomputed pattern, so matches() can take a fast path.
_EXACT = 0  # no wildcards: plain string comparison
_FIXED = 1  # only "*" wildcards: one topic segment per pattern segment
_MULTI = 2  # at least one "**": segment-wise NFA


def _describe(value: str) -> str:
    """repr() of a (possibly hostile) value, truncated for error messages."""
    return repr(value if len(value) <= 64 else value[:64] + "...")


def validate_topic(topic: str) -> str:
    """Return *topic* unchanged if it is a well-formed topic.

    A topic is one or more dot-separated segments of ASCII letters, digits,
    ``_``, ``-`` and ``:`` -- no whitespace, no CR/LF, no empty segments, no
    wildcards -- and at most :data:`MAX_TOPIC_LENGTH` characters long.

    :raises InvalidTopicError: if the topic does not satisfy these rules.
    """
    if not isinstance(topic, str):
        raise InvalidTopicError(f"topic must be a str, not {type(topic).__name__}")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise InvalidTopicError(
            f"topic is {len(topic)} characters long; the maximum is {MAX_TOPIC_LENGTH}"
        )
    if _TOPIC_RE.fullmatch(topic) is None:
        raise InvalidTopicError(
            f"invalid topic {_describe(topic)}: expected dot-separated segments of "
            "letters, digits, '_', '-' or ':'"
        )
    return topic


def validate_pattern(pattern: str) -> str:
    """Return *pattern* unchanged if it is a well-formed topic pattern.

    Same rules as :func:`validate_topic`, except that a whole segment may also
    be ``*`` (exactly one segment) or ``**`` (one or more segments), and the
    pattern may have at most :data:`MAX_PATTERN_SEGMENTS` segments. Partial
    wildcards such as ``orders*`` are rejected.

    :raises InvalidTopicError: if the pattern does not satisfy these rules.
    """
    if not isinstance(pattern, str):
        raise InvalidTopicError(f"topic pattern must be a str, not {type(pattern).__name__}")
    if len(pattern) > MAX_TOPIC_LENGTH:
        raise InvalidTopicError(
            f"topic pattern is {len(pattern)} characters long; the maximum is {MAX_TOPIC_LENGTH}"
        )
    segments = pattern.split(".")
    if len(segments) > MAX_PATTERN_SEGMENTS:
        raise InvalidTopicError(
            f"topic pattern has {len(segments)} segments; the maximum is {MAX_PATTERN_SEGMENTS}"
        )
    for segment in segments:
        if segment not in _WILDCARDS and _SEGMENT_RE.fullmatch(segment) is None:
            raise InvalidTopicError(
                f"invalid topic pattern {_describe(pattern)}: segment {_describe(segment)} "
                "must be '*', '**' or letters, digits, '_', '-' or ':'"
            )
    return pattern


def _is_clean(topic: str) -> bool:
    """False for topics that must never match: empty, whitespace or control chars."""
    # str.isprintable() is False for every control character (CR, LF, NUL,
    # ...) and every Unicode separator except the ASCII space.
    return bool(topic) and topic.isprintable() and " " not in topic


def _match_multi(pattern: tuple[str, ...], topic: list[str]) -> bool:
    """Segment-wise match of a pattern containing ``**``.

    Simulates the pattern as an NFA whose state ``i`` means "the first *i*
    pattern segments matched the topic segments consumed so far". Each topic
    segment is consumed once and every state is visited at most once per
    segment, so the cost is O(len(pattern) x len(topic)) -- no backtracking.
    ``**`` consumes one segment and either stays (to consume more) or moves on,
    which is exactly "one or more segments".
    """
    final = len(pattern)
    states = {0}
    for segment in topic:
        next_states: set[int] = set()
        for i in states:
            if i == final:
                continue
            expected = pattern[i]
            if expected == MULTI_WILDCARD:
                if segment:
                    next_states.add(i)
                    next_states.add(i + 1)
            elif expected == SINGLE_WILDCARD:
                if segment:
                    next_states.add(i + 1)
            elif expected == segment:
                next_states.add(i + 1)
        if not next_states:
            return False
        states = next_states
    return final in states


@dataclass(frozen=True, slots=True)
class TopicPattern:
    """
    Glob-style topic pattern for filtering.

    Supports::

        "orders.*"          -- matches "orders.created", "orders.updated"
        "orders.**"         -- matches "orders.created", "orders.item.added"
        "payments.refund.*" -- matches "payments.refund.completed"
        "orders.created"    -- exact match

    Matching is segment-wise (topics are split on ``.``): a literal segment
    matches the identical segment, ``*`` matches exactly one non-empty segment
    and ``**`` matches one or more non-empty segments. A topic that is empty
    or contains whitespace or control characters (CR, LF, ...) never matches.

    The pattern itself is not validated here, because server-side code may use
    any pattern it likes; validate client-supplied patterns with
    :func:`validate_pattern` (the SSE and WebSocket adapters do).
    """

    pattern: str
    # Precomputed once per instance; not part of eq/hash/repr.
    _segments: tuple[str, ...] = field(init=False, repr=False, compare=False, hash=False)
    _kind: int = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        segments = tuple(self.pattern.split("."))
        if MULTI_WILDCARD in segments:
            kind = _MULTI
        elif SINGLE_WILDCARD in segments:
            kind = _FIXED
        else:
            kind = _EXACT
        object.__setattr__(self, "_segments", segments)
        object.__setattr__(self, "_kind", kind)

    def matches(self, topic: str) -> bool:
        """
        Return whether *topic* matches this pattern.

        Runs in O(pattern segments x topic segments) time, without regular
        expressions, so a hostile topic cannot stall the event loop.

        :param topic: The topic to test, e.g. ``"orders.created"``.
        :return: True if the topic matches the pattern, False otherwise.
        """
        if not _is_clean(topic):
            return False
        if self._kind == _EXACT:
            return topic == self.pattern
        topic_segments = topic.split(".")
        if self._kind == _FIXED:
            if len(topic_segments) != len(self._segments):
                return False
            return all(
                (expected == SINGLE_WILDCARD and segment != "") or expected == segment
                for expected, segment in zip(self._segments, topic_segments, strict=True)
            )
        # Every pattern segment consumes at least one topic segment.
        if len(topic_segments) < len(self._segments):
            return False
        return _match_multi(self._segments, topic_segments)

    @staticmethod
    def exact(topic: str) -> TopicPattern:
        return TopicPattern(pattern=topic)


def topic_to_stream_key(topic: str, prefix: str = "eventbus") -> str:
    """Convert a topic to a Redis stream key.

    ``"orders.created"`` -> ``"eventbus:stream:orders.created"``
    """
    return f"{prefix}:stream:{topic}"


def topic_to_channel(topic: str, prefix: str = "eventbus") -> str:
    """Convert a topic to a Redis Pub/Sub channel.

    ``"orders.created"`` -> ``"eventbus:channel:orders.created"``
    """
    return f"{prefix}:channel:{topic}"
