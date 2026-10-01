import time
from dataclasses import FrozenInstanceError, fields

import pytest

from event_bus.core.errors import EventBusError, InvalidTopicError
from event_bus.core.topic import (
    MAX_PATTERN_SEGMENTS,
    MAX_TOPIC_LENGTH,
    TopicPattern,
    topic_to_channel,
    topic_to_stream_key,
    validate_pattern,
    validate_topic,
)


class TestTopicPattern:
    def test_exact_match(self) -> None:
        p = TopicPattern("orders.created")
        assert p.matches("orders.created")
        assert not p.matches("orders.updated")
        assert not p.matches("orders")

    def test_single_wildcard(self) -> None:
        p = TopicPattern("orders.*")
        assert p.matches("orders.created")
        assert p.matches("orders.updated")
        assert not p.matches("orders.item.added")
        assert not p.matches("payments.created")

    def test_double_wildcard(self) -> None:
        p = TopicPattern("orders.**")
        assert p.matches("orders.created")
        assert p.matches("orders.item.added")
        assert p.matches("orders.item.sub.deep")
        assert not p.matches("payments.created")

    def test_middle_wildcard(self) -> None:
        p = TopicPattern("app.*.completed")
        assert p.matches("app.orders.completed")
        assert p.matches("app.payments.completed")
        assert not p.matches("app.orders.items.completed")

    def test_exact_factory(self) -> None:
        p = TopicPattern.exact("orders.created")
        assert p.matches("orders.created")
        assert not p.matches("orders.updated")

    def test_catch_all_pattern(self) -> None:
        """'**' matches any topic."""
        p = TopicPattern("**")
        assert p.matches("orders.created")
        assert p.matches("a")
        assert p.matches("a.b.c.d.e")

    def test_segments_precomputed_per_instance(self) -> None:
        """Segments are computed once per instance; no module-level cache grows."""
        import event_bus.core.topic as topic_module

        assert not hasattr(topic_module, "_pattern_cache")
        p = TopicPattern("cache.test.*")
        assert p._segments == ("cache", "test", "*")
        assert p.matches("cache.test.a")
        assert p._segments == ("cache", "test", "*")

    def test_private_fields_not_in_eq_hash_repr(self) -> None:
        assert TopicPattern("a.*") == TopicPattern("a.*")
        assert TopicPattern("a.*") != TopicPattern("a.**")
        assert hash(TopicPattern("a.*")) == hash(TopicPattern("a.*"))
        assert repr(TopicPattern("a.*")) == "TopicPattern(pattern='a.*')"
        assert len({TopicPattern("a.*"), TopicPattern("a.*"), TopicPattern("b")}) == 2
        assert [f.name for f in fields(TopicPattern) if f.init] == ["pattern"]

    def test_frozen(self) -> None:
        p = TopicPattern("a.*")
        with pytest.raises(FrozenInstanceError):
            p.pattern = "b"  # type: ignore[misc]

    def test_special_characters_in_topic(self) -> None:
        """Dots and hyphens handled correctly."""
        p = TopicPattern("my-app.orders-v2.*")
        assert p.matches("my-app.orders-v2.created")
        assert not p.matches("my-app.orders-v2.sub.deep")
        assert not p.matches("myXapp.ordersXv2.created")

    def test_mixed_wildcard_pattern(self) -> None:
        """'a.**.b.*' works correctly."""
        p = TopicPattern("a.**.b.*")
        assert p.matches("a.x.b.y")
        assert p.matches("a.x.y.b.z")
        assert not p.matches("a.b.y")  # ** must match at least one segment

    def test_multi_wildcard_needs_at_least_one_segment(self) -> None:
        p = TopicPattern("orders.**")
        assert not p.matches("orders")
        assert not p.matches("orders.")
        assert TopicPattern("**.created").matches("a.b.created")
        assert not TopicPattern("**.created").matches("created")

    def test_consecutive_multi_wildcards(self) -> None:
        p = TopicPattern("a.**.**")
        assert not p.matches("a.b")  # each ** needs its own segment
        assert p.matches("a.b.c")
        assert p.matches("a.b.c.d.e")

    def test_single_wildcard_needs_non_empty_segment(self) -> None:
        assert not TopicPattern("orders.*").matches("orders.")
        assert not TopicPattern("*.x").matches(".x")
        assert not TopicPattern("**").matches("a..b")

    def test_trailing_newline_never_matches(self) -> None:
        """Regression: the old ``^...$`` regex matched before a trailing newline."""
        assert not TopicPattern("orders.created").matches("orders.created\n")
        assert not TopicPattern("orders.*").matches("orders.created\n")
        assert not TopicPattern("orders.**").matches("orders.created\n")
        assert not TopicPattern("**").matches("orders.created\n")

    @pytest.mark.parametrize(
        "topic",
        [
            "",
            "orders.created\r",
            "orders.cre ated",
            "orders.\tcreated",
            "orders.created\nevent: admin",
            "orders.\x00",
            "orders.\u2028x",
            "orders.\u00a0x",
        ],
    )
    def test_whitespace_and_control_characters_never_match(self, topic: str) -> None:
        for pattern in ("**", "orders.*", "orders.**", "*.*", topic):
            assert not TopicPattern(pattern).matches(topic)

    def test_pathological_pattern_is_fast(self) -> None:
        """Regression: nested ``**`` backtracked exponentially (1.8 s on a 63-char topic)."""
        pattern = TopicPattern(".".join(["a"] + ["**"] * 30 + ["b"]))
        topic = ".".join(["a"] * 127)  # 253 chars, never ends in "b"
        t0 = time.perf_counter()
        assert not pattern.matches(topic)
        assert pattern.matches(topic + ".b")
        assert time.perf_counter() - t0 < 0.05

    def test_many_patterns_many_topics_is_fast(self) -> None:
        patterns = [TopicPattern(p) for p in ("**.x.**.y", "a.**.**.**.z", "*.*.**")]
        topics = [".".join(["a"] * n) for n in range(1, 128)]
        t0 = time.perf_counter()
        for p in patterns:
            for t in topics:
                p.matches(t)
        assert time.perf_counter() - t0 < 0.5


class TestValidateTopic:
    @pytest.mark.parametrize(
        "topic",
        ["orders", "orders.created", "my-app.orders_v2.created", "tenant:42.orders", "A.b.C9"],
    )
    def test_valid(self, topic: str) -> None:
        assert validate_topic(topic) == topic

    def test_max_length(self) -> None:
        topic = "a" * MAX_TOPIC_LENGTH
        assert validate_topic(topic) == topic
        with pytest.raises(InvalidTopicError, match="maximum"):
            validate_topic(topic + "a")

    @pytest.mark.parametrize(
        "topic",
        [
            "",
            ".",
            "orders.",
            ".orders",
            "orders..created",
            "orders created",
            "orders.created\n",
            "orders.created\r\nevent: admin",
            "orders\t",
            "orders.*",
            "orders.**",
            "orders/created",
            "commandes.créées",
        ],
    )
    def test_invalid(self, topic: str) -> None:
        with pytest.raises(InvalidTopicError):
            validate_topic(topic)

    def test_not_a_string(self) -> None:
        with pytest.raises(InvalidTopicError):
            validate_topic(42)  # type: ignore[arg-type]

    def test_error_is_value_error_and_event_bus_error(self) -> None:
        with pytest.raises(ValueError):
            validate_topic("bad topic")
        with pytest.raises(EventBusError):
            validate_topic("bad topic")

    def test_error_message_escapes_control_characters(self) -> None:
        with pytest.raises(InvalidTopicError) as exc_info:
            validate_topic("x\nevent: admin")
        assert "\n" not in str(exc_info.value)  # repr()-escaped, safe to log


class TestValidatePattern:
    @pytest.mark.parametrize(
        "pattern",
        ["orders.*", "orders.**", "**", "*", "a.*.b.**", "orders.created", "t-1:x.**.y_z"],
    )
    def test_valid(self, pattern: str) -> None:
        assert validate_pattern(pattern) == pattern

    @pytest.mark.parametrize(
        "pattern",
        [
            "",
            "orders.",
            "orders..*",
            "orders*",
            "*orders",
            "orders.***",
            "orders.*\n",
            "orders. *",
            "orders.?",
        ],
    )
    def test_invalid(self, pattern: str) -> None:
        with pytest.raises(InvalidTopicError):
            validate_pattern(pattern)

    def test_segment_limit(self) -> None:
        ok = ".".join(["a"] * MAX_PATTERN_SEGMENTS)
        assert validate_pattern(ok) == ok
        with pytest.raises(InvalidTopicError, match="segments"):
            validate_pattern(ok + ".a")

    def test_length_limit(self) -> None:
        with pytest.raises(InvalidTopicError, match="maximum"):
            validate_pattern("a" * (MAX_TOPIC_LENGTH + 1))

    def test_not_a_string(self) -> None:
        with pytest.raises(InvalidTopicError):
            validate_pattern(None)  # type: ignore[arg-type]


class TestKeyHelpers:
    def test_stream_key(self) -> None:
        assert topic_to_stream_key("orders.created") == "eventbus:stream:orders.created"
        assert topic_to_stream_key("orders.created", "myapp") == "myapp:stream:orders.created"

    def test_channel_key(self) -> None:
        assert topic_to_channel("orders.created") == "eventbus:channel:orders.created"
        assert topic_to_channel("orders.created", "myapp") == "myapp:channel:orders.created"
