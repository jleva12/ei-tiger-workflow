"""Regular expressions that come with documents, on the bounded engine
(forge_common.patterns): each use held to its time, off the interpreter lock;
one too big to compile quickly refused before it's compiled; and a JSON
Schema's every keyword that matches a pattern checked on it, a value's whole
check held to one time, its patterns among it, and its references looked up
within it, never fetched.

A regression to Python's re, or to a check that isn't held to its time, would
hang these tests rather than fail them, so those that would are held to a
timeout (pytest-timeout's signal, which re answers)."""

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest
from jsonschema import Draft202012Validator

from forge_common import patterns
from forge_common.patterns import (
    InvalidPattern,
    PatternTimeout,
    SchemaValidator,
    compiled,
    schema_errors,
)

RUNAWAY = "^(a|aa)+$"
LONG = "a" * 60 + "b"


def test_each_use_of_a_pattern_is_held_to_its_time() -> None:
    pattern = compiled(RUNAWAY, seconds=0.05)
    for use in (
        lambda: pattern.search(LONG),
        lambda: list(pattern.finditer(LONG)),
        lambda: pattern.sub("x", LONG),
        lambda: pattern.split(LONG),
    ):
        began = time.perf_counter()
        with pytest.raises(PatternTimeout, match=r"took longer than 50 ms"):
            use()
        assert time.perf_counter() - began < 0.5


def test_a_pattern_lets_other_threads_run_while_it_matches() -> None:
    ticks: list[float] = []
    stop = threading.Event()

    def ticking() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            time.sleep(0.002)

    thread = threading.Thread(target=ticking)
    thread.start()
    try:
        with pytest.raises(PatternTimeout):
            compiled(RUNAWAY, seconds=0.3).search(LONG)
    finally:
        stop.set()
        thread.join()

    assert len(ticks) > 20 and max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.1


def test_a_pattern_reads_like_pythons() -> None:
    pattern = compiled(r"(\w)(\d)", seconds=1)
    assert (pattern.pattern, pattern.groups, str(pattern)) == (r"(\w)(\d)", 2, r"/(\w)(\d)/")
    assert pattern.sub(lambda m: m.group(2) + m.group(1), "a1 b2") == "1a 2b"
    assert compiled("HELLO", seconds=1, ignore_case=True).search("say hello") is not None
    with pytest.raises(InvalidPattern, match="doesn't compile"):
        compiled("a{2,1}", seconds=1)
    with pytest.raises(InvalidPattern, match="isn't text"):
        compiled(7, seconds=1)  # type: ignore[arg-type]  # a schema's pattern is any JSON


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "pattern",
    [
        "a{100000000}",
        "((a{1000}){1000}){1000}",
        "(?:x{3}|y{3}){1000000}",
        "(a{1000})(?#a comment){1000}",  # a comment between a part and its repeat
        r"(a{1000})(?#\)){1000}",  # an escaped ) doesn't end the comment
        "[(](a{1000}){1000}",  # a ( in a set doesn't open a group
        r"\((a{1000}){1000}",  # nor does an escaped one
        "[]((]{1000}(a{1000}){1000}",  # a ] first in a set is a member
        "[[:alpha:](](a{1000}){1000}",  # as is a POSIX class
        "(a{1000})(?i){1000}",  # flags between a part and its repeat are nothing too
        "(?:" + "{}" * 500 + "){1000}",  # {} is two characters, not a repeat
        "(a)(?:" + "(?1)" * 300 + "){1000}",  # a call is a part, not flags
        "(?x)a{10}",  # verbose mode's comments aren't read
        "(?V1)a",  # nor VERSION1's nested sets
    ],
)
def test_a_pattern_too_big_to_compile_quickly_is_refused_before_it_compiles(
    pattern: str,
) -> None:
    began = time.perf_counter()
    with pytest.raises(InvalidPattern, match="doesn't compile: "):
        compiled(pattern, seconds=1)
    assert time.perf_counter() - began < 0.5


@pytest.mark.parametrize(
    "pattern",
    [
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$",
        "^.{0,100000000}$",  # a repeat's most doesn't count, only its least
        "(?:a{2}){20000}",
        r"^\p{L}{2,}$",
        "(?i)^[]a-z]{3}$",
        "a(?#{1000000}){3}",
    ],
)
def test_a_pattern_of_any_usual_size_compiles(pattern: str) -> None:
    assert compiled(pattern, seconds=1).pattern == pattern


def test_a_schemas_patterns_are_held_to_their_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patterns, "SCHEMA_SECONDS", 0.05)
    validator = SchemaValidator(
        {
            "type": "object",
            "properties": {"code": {"type": "string", "pattern": RUNAWAY}},
            "patternProperties": {RUNAWAY: {"type": "integer"}},
        }
    )

    assert [error.message for error in validator.iter_errors({"code": "aaaa"})] == []
    assert [error.message for error in validator.iter_errors({"code": "b"})] == [
        "'b' does not match '^(a|aa)+$'"
    ]
    began = time.perf_counter()
    said = [error.message for error in validator.iter_errors({"code": LONG, LONG: "x"})]
    assert len(said) == 2 and all("took longer than 50 ms" in message for message in said)
    assert time.perf_counter() - began < 1.0
    # A property a pattern names is held to its schema.
    assert [error.message for error in validator.iter_errors({"aa": "x"})] == [
        "'x' is not of type 'integer'"
    ]


DRAFT_7 = "http://json-schema.org/draft-07/schema#"
DRAFT_2020 = "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"pattern": RUNAWAY}, LONG),
        ({"patternProperties": {RUNAWAY: {}}}, {LONG: 1}),
        ({"patternProperties": {RUNAWAY: {}}, "additionalProperties": False}, {LONG: 1}),
        ({"patternProperties": {RUNAWAY: {}}, "unevaluatedProperties": False}, {LONG: 1}),
        (
            {"allOf": [{"patternProperties": {RUNAWAY: {}}}], "unevaluatedProperties": False},
            {LONG: 1},
        ),
        ({"propertyNames": {"pattern": RUNAWAY}}, {LONG: 1}),
        ({"items": {"pattern": RUNAWAY}}, [LONG]),
        ({"if": {"pattern": RUNAWAY}, "then": False}, LONG),
        # A subschema naming its draft is still checked here, not by that draft's validator.
        ({"properties": {"code": {"$schema": DRAFT_2020, "pattern": RUNAWAY}}}, {"code": LONG}),
        ({"properties": {"code": {"$schema": DRAFT_7, "pattern": RUNAWAY}}}, {"code": LONG}),
        (
            {
                "$defs": {"code": {"$schema": DRAFT_7, "pattern": RUNAWAY}},
                "properties": {"code": {"$ref": "#/$defs/code"}},
            },
            {"code": LONG},
        ),
    ],
)
def test_every_keyword_that_matches_a_pattern_is_held_to_its_time(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any], value: Any
) -> None:
    monkeypatch.setattr(patterns, "SCHEMA_SECONDS", 0.05)
    began = time.perf_counter()
    said = [error.message for error in schema_errors(schema, value)]
    assert said and any("took longer than the 50 ms" in message for message in said), said
    assert time.perf_counter() - began < 0.5


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    ("schema", "value", "where"),
    [
        ({"type": "array", "items": {"type": "string", "pattern": RUNAWAY}}, [LONG] * 40, [0]),
        # Nor is each of 500 more patterns tried on each of its names.
        (
            {"patternProperties": {RUNAWAY: {}, **{f"^n{i}$": {} for i in range(500)}}},
            {LONG: 1, **{f"n{i}": i for i in range(500)}},
            [],
        ),
    ],
)
def test_all_of_a_values_patterns_share_its_time(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any], value: Any, where: list[Any]
) -> None:
    monkeypatch.setattr(patterns, "SCHEMA_SECONDS", 0.05)

    began = time.perf_counter()
    errors = schema_errors(schema, value)

    assert time.perf_counter() - began < 0.3  # not 50 ms for each
    # The first pattern takes all the time there is: no more of the value is checked.
    assert [(list(error.absolute_path), error.message) for error in errors] == [
        (
            where,
            "its regular expression /^(a|aa)+$/ took longer than the 50 ms "
            "a value's regular expressions get",
        )
    ]
    assert patterns._CHECK.get() is None  # the check's over


@pytest.mark.timeout(10)
def test_a_pattern_that_cant_be_run_refuses_the_value_wherever_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(patterns, "SCHEMA_SECONDS", 0.05)
    # Not matching would fit: whether it does can't be told in time.
    for schema in (
        {"not": {"pattern": RUNAWAY}},
        {"anyOf": [{"pattern": RUNAWAY}, {"type": "string"}]},
        {"if": {"pattern": RUNAWAY}, "then": {"type": "number"}},
    ):
        assert [error.message for error in schema_errors(schema, LONG)] == [
            "its regular expression /^(a|aa)+$/ took longer than the 50 ms "
            "a value's regular expressions get"
        ]
    assert [error.message for error in schema_errors({"not": {"pattern": "a{2,1}"}}, "b")] == [
        "its regular expression /a{2,1}/ doesn't compile: min repeat greater than max repeat "
        "at position 2"
    ]
    assert schema_errors({"not": {"pattern": "^b"}}, LONG) == []


def levels(level: Callable[[str], Any], depth: int = 30) -> dict[str, Any]:
    """``$defs`` of ``depth`` levels from ``l0``, each one checking a value
    against the next twice over as ``level`` refers to it, the last a
    string's: as many as 2**depth checks in all, unless the value's time
    holds them."""
    defs = {f"l{i}": level(f"#/$defs/l{i + 1}") for i in range(depth)}
    return {**defs, f"l{depth}": {"type": "string"}}


ANY_OF = levels(lambda ref: {"anyOf": [{"$ref": ref}, {"$ref": ref}]})
ALL_OF = levels(lambda ref: {"allOf": [{"$ref": ref}, {"$ref": ref}]})
ONE_OF = levels(lambda ref: {"oneOf": [{"$ref": ref}, {"$ref": ref}]})
BOTH_REFS = levels(lambda ref: {"$ref": ref, "$dynamicRef": ref})
L0 = "#/$defs/l0"
OUT_OF_TIME = "checking it took longer than the 50 ms a value gets"


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"$defs": ANY_OF, "$ref": L0}, 5),
        ({"$defs": ALL_OF, "$ref": L0}, "x"),  # it fits, if that could be told
        ({"$defs": ONE_OF, "$ref": L0}, "x"),
        ({"$defs": BOTH_REFS, "$ref": L0}, "x"),
        # Where not fitting would fit.
        ({"$defs": ANY_OF, "not": {"$ref": L0}}, 5),
        ({"$defs": ANY_OF, "anyOf": [{"$ref": L0}, {}]}, 5),
        ({"$defs": ANY_OF, "if": {"$ref": L0}, "then": False}, 5),
        # Where what a schema evaluates is told, before the value's checked against it.
        ({"unevaluatedProperties": False, "$defs": BOTH_REFS, "$ref": L0}, {"a": 1}),
        ({"unevaluatedItems": False, "$defs": BOTH_REFS, "$ref": L0}, [1]),
        # One validator checking each of a great many items.
        ({"contains": {"type": "string"}}, [0] * 1_000_000),
    ],
)
def test_a_values_whole_check_is_held_to_its_time(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any], value: Any
) -> None:
    monkeypatch.setattr(patterns, "SCHEMA_SECONDS", 0.05)

    began = time.perf_counter()
    said = [error.message for error in schema_errors(schema, value)]

    assert time.perf_counter() - began < 0.3
    assert said == [OUT_OF_TIME]
    assert patterns._CHECK.get() is None


@pytest.mark.timeout(10)
def test_what_a_check_finds_in_time_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patterns, "SCHEMA_SECONDS", 0.05)
    schema = {
        "$defs": ANY_OF,
        "properties": {
            "a": {"type": "integer"},
            "b": {"$ref": L0},
            "c": {"type": "integer"},  # the time's up before it's checked
        },
    }

    errors = schema_errors(schema, {"a": "x", "b": 5, "c": "y"})

    assert [(list(error.absolute_path), error.message) for error in errors] == [
        (["a"], "'x' is not of type 'integer'"),
        ([], OUT_OF_TIME),
    ]


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"pattern": "^x-"}, "y"),
        ({"patternProperties": {"^x-": {"type": "integer"}}}, {"x-a": "no", "b": 1}),
        (
            {"patternProperties": {"^x-": {}, "^y-": {}}, "additionalProperties": False},
            {"x-a": 1, "b": 2, "c": 3},
        ),
        (
            {"properties": {"a": {}}, "patternProperties": {}, "additionalProperties": False},
            {"b": 1},
        ),
        ({"properties": {"a": {}}, "additionalProperties": False}, {"a": 1, "b": 2, "c": 3}),
        (
            {
                "patternProperties": {"^x-": {"type": "integer"}},
                "additionalProperties": {"type": "string"},
            },
            {"x-a": "no", "b": 2, "c": "yes"},
        ),
        (
            {
                "allOf": [{"patternProperties": {"^x-": {}}}],
                "properties": {"a": {}},
                "unevaluatedProperties": False,
            },
            {"a": 1, "x-b": 2, "c": 3, "d": 4},
        ),
        (
            {"patternProperties": {"^x-": {}}, "unevaluatedProperties": {"type": "integer"}},
            {"x-a": "s", "b": "t", "c": 1},
        ),
        (
            {
                "$defs": {"x": {"patternProperties": {"^x-": {}}}},
                "$ref": "#/$defs/x",
                "unevaluatedProperties": False,
            },
            {"x-a": 1, "b": 2},
        ),
        (
            {
                "dependentSchemas": {"kind": {"patternProperties": {"^x-": {}}}},
                "properties": {"kind": {}},
                "unevaluatedProperties": False,
            },
            {"kind": 1, "x-a": 2, "b": 3},
        ),
        (
            {"if": {"properties": {"k": {"pattern": "^a"}}}, "then": {"required": ["v"]}},
            {"k": "abc"},
        ),
        ({"propertyNames": {"pattern": "^[a-z]+$"}}, {"Ab": 1, "ok": 2}),
        ({"properties": {"a": {"$schema": DRAFT_2020, "pattern": "^x"}}}, {"a": "y"}),
        # References within the schema, and to the drafts' meta-schemas.
        ({"$defs": {"t": {"$anchor": "t", "type": "string"}}, "items": {"$ref": "#t"}}, ["a", 1]),
        (
            {
                "$id": "https://example.com/root.json",
                "$defs": {"t": {"$id": "t.json", "type": "string"}},
                "$ref": "https://example.com/t.json",
            },
            1,
        ),
        (
            {
                "$id": "https://example.com/strings",
                "$ref": "list",
                "$defs": {
                    "items": {"$dynamicAnchor": "items", "type": "string"},
                    "list": {
                        "$id": "list",
                        "items": {"$dynamicRef": "#items"},
                        "$defs": {"items": {"$dynamicAnchor": "items"}},
                    },
                },
            },
            ["a", 1],
        ),
        (
            {"$defs": {"a": {"prefixItems": [{}]}}, "$ref": "#/$defs/a", "unevaluatedItems": False},
            [1, 2],
        ),
        ({"$ref": DRAFT_2020}, {"type": 5, "minLength": -1}),
        ({"$ref": DRAFT_7}, {"type": 5}),
        # The items unevaluatedItems leaves, told as jsonschema tells them.
        (
            {
                "prefixItems": [{}],
                "if": {"prefixItems": [{}, {}]},
                "then": {"prefixItems": [{}, {}, {}]},
                "contains": {"type": "string"},
                "unevaluatedItems": False,
            },
            [1, 2, 3, 4, "s", 6],
        ),
        (
            {
                "allOf": [{"prefixItems": [{}]}],
                "anyOf": [{"prefixItems": [{}, {}]}, {"items": {"type": "string"}}],
                "unevaluatedItems": {"type": "integer"},
            },
            [1, 2, "x", 3],
        ),
        # References that come back round going into the value, or beside
        # themselves rather than inside.
        (
            {
                "$defs": {
                    "node": {
                        "type": "object",
                        "properties": {"children": {"items": {"$ref": "#/$defs/node"}}},
                    }
                },
                "$ref": "#/$defs/node",
            },
            {"children": [{"children": []}, {"children": [{"children": [5]}]}]},
        ),
        ({"$defs": {"s": {"type": "string"}}, "allOf": [{"$ref": "#/$defs/s"}] * 2}, 1),
        # One inside itself where what takes only the first error (not, if)
        # has had one: it stops there.
        (
            {"$defs": {"t": {"type": "string", "not": {"$ref": "#/$defs/t"}}}, "$ref": "#/$defs/t"},
            1,
        ),
        (
            {
                "$defs": {"t": {"type": "string", "if": {"$ref": "#/$defs/t"}}},
                "$ref": "#/$defs/t",
                "unevaluatedProperties": False,
            },
            {"a": 1},
        ),
    ],
)
def test_the_keywords_mean_what_jsonschemas_do(schema: dict[str, Any], value: Any) -> None:
    def said(errors: Any) -> list[tuple[list[Any], str]]:
        return sorted((list(error.absolute_path), error.message) for error in errors)

    expected = said(Draft202012Validator(schema).iter_errors(value))
    assert said(SchemaValidator(schema).iter_errors(value)) == expected
    assert said(schema_errors(schema, value)) == expected


def refused(keyword: str, reference: str) -> str:
    return (
        f"its {keyword} {reference!r} can't be resolved: only references within the schema are read"
    )


@pytest.fixture
def schema_host() -> Iterator[tuple[str, list[str]]]:
    """A web server answering any path with a schema nothing fits
    (``false``), on this machine; and the paths it's asked for."""
    asked: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            asked.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/schema+json")
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"false")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", asked
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.timeout(10)
def test_a_schemas_references_elsewhere_are_never_fetched(
    schema_host: tuple[str, list[str]], tmp_path: Path
) -> None:
    host, asked = schema_host
    elsewhere = f"{host}/schema.json"
    on_disk = tmp_path / "schema.json"
    on_disk.write_text("false")  # nothing would fit it, read

    for schema, value, said in (
        ({"$ref": elsewhere}, 1, refused("$ref", elsewhere)),
        ({"$dynamicRef": elsewhere}, 1, refused("$dynamicRef", elsewhere)),
        ({"$id": elsewhere, "$ref": "other.json"}, 1, refused("$ref", "other.json")),
        ({"$ref": on_disk.as_uri()}, 1, refused("$ref", on_disk.as_uri())),
        # Where not fitting it would fit.
        ({"not": {"$ref": elsewhere}}, 1, refused("$ref", elsewhere)),
        ({"anyOf": [{"$ref": elsewhere}, {}]}, 1, refused("$ref", elsewhere)),
        # Where jsonschema looks it up again, for what it evaluates.
        ({"$ref": elsewhere, "unevaluatedProperties": False}, {"a": 1}, refused("$ref", elsewhere)),
        (
            {"$dynamicRef": elsewhere, "unevaluatedItems": False},
            [1],
            refused("$dynamicRef", elsewhere),
        ),
    ):
        assert [error.message for error in schema_errors(schema, value)] == [said], schema
    # The validator itself, built without a registry of its own.
    assert [error.message for error in SchemaValidator({"$ref": elsewhere}).iter_errors(1)] == [
        refused("$ref", elsewhere)
    ]

    assert asked == []
    with urlopen(elsewhere, timeout=5) as answered:  # the server answers whoever does ask
        assert json.load(answered) is False
    assert asked == ["/schema.json"]


MISSING = "#/$defs/missing"


@pytest.mark.parametrize(
    ("schema", "value", "said"),
    [
        ({"$ref": MISSING}, 1, [([], refused("$ref", MISSING))]),
        ({"$ref": "#missing"}, 1, [([], refused("$ref", "#missing"))]),
        ({"$dynamicRef": "#missing"}, 1, [([], refused("$dynamicRef", "#missing"))]),
        (
            {"$defs": {"a": {"$ref": MISSING}}, "$ref": "#/$defs/a"},
            1,
            [([], refused("$ref", MISSING))],
        ),
        ({"properties": {"a": {"$ref": MISSING}}}, {"a": 1}, [(["a"], refused("$ref", MISSING))]),
        # Where not fitting it would fit.
        ({"not": {"$ref": MISSING}}, 1, [([], refused("$ref", MISSING))]),
        ({"anyOf": [{"$ref": MISSING}, {}]}, 1, [([], refused("$ref", MISSING))]),
        ({"if": {"$ref": MISSING}, "then": False}, 1, [([], refused("$ref", MISSING))]),
        # What it would evaluate can't be told, so nothing's said to be unevaluated.
        (
            {"unevaluatedProperties": False, "$ref": MISSING},
            {"a": 1},
            [([], refused("$ref", MISSING))],
        ),
        ({"unevaluatedItems": False, "$ref": MISSING}, [1], [([], refused("$ref", MISSING))]),
        # A pointer through what has no such part, or a URL that doesn't parse.
        ({"allOf": [{}], "$ref": "#/allOf/x"}, 1, [([], refused("$ref", "#/allOf/x"))]),
        ({"minLength": 1, "$ref": "#/minLength/x"}, "a", [([], refused("$ref", "#/minLength/x"))]),
        ({"$ref": "http://[::1#x"}, 1, [([], refused("$ref", "http://[::1#x"))]),
    ],
)
def test_a_reference_that_cant_be_resolved_refuses_the_value(
    schema: dict[str, Any], value: Any, said: list[tuple[list[Any], str]]
) -> None:
    errors = schema_errors(schema, value)
    assert [(list(error.absolute_path), error.message) for error in errors] == said
    assert patterns._CHECK.get() is None


def not_one(keyword: str, reference: Any) -> str:
    return f"its {keyword} {reference!r} isn't a reference"


def not_to_a_schema(keyword: str, reference: str) -> str:
    return f"its {keyword} {reference!r} doesn't refer to a schema"


@pytest.mark.parametrize(
    ("schema", "value", "said"),
    [
        ({"$ref": 5}, 1, [([], not_one("$ref", 5))]),
        ({"$ref": None}, 1, [([], not_one("$ref", None))]),
        ({"$ref": ["#"]}, 1, [([], not_one("$ref", ["#"]))]),
        ({"$dynamicRef": {"$ref": "#"}}, 1, [([], not_one("$dynamicRef", {"$ref": "#"}))]),
        ({"properties": {"a": {"$ref": 5}}}, {"a": 1}, [(["a"], not_one("$ref", 5))]),
        # Where not fitting it would fit.
        ({"not": {"$ref": 5}}, 1, [([], not_one("$ref", 5))]),
        ({"anyOf": [{"$ref": 5}, {}]}, 1, [([], not_one("$ref", 5))]),
        ({"if": {"$ref": 5}, "then": False}, 1, [([], not_one("$ref", 5))]),
        # What it would evaluate can't be told, so nothing's said to be unevaluated.
        ({"unevaluatedProperties": False, "$ref": 5}, {"a": 1}, [([], not_one("$ref", 5))]),
        ({"$ref": 5, "unevaluatedProperties": False}, {"a": 1}, [([], not_one("$ref", 5))]),
        ({"unevaluatedItems": False, "$dynamicRef": 5}, [1], [([], not_one("$dynamicRef", 5))]),
        ({"$ref": 5, "unevaluatedItems": False}, [1], [([], not_one("$ref", 5))]),
        # One to what's in the schema but isn't a schema.
        (
            {"minLength": 1, "$ref": "#/minLength"},
            "a",
            [([], not_to_a_schema("$ref", "#/minLength"))],
        ),
        (
            {"required": ["a"], "$ref": "#/required/0"},
            {"a": 1},
            [([], not_to_a_schema("$ref", "#/required/0"))],
        ),
        (
            {"minLength": 1, "not": {"$ref": "#/minLength"}},
            "a",
            [([], not_to_a_schema("$ref", "#/minLength"))],
        ),
    ],
)
def test_a_reference_that_isnt_one_or_isnt_to_a_schema_refuses_the_value(
    schema: dict[str, Any], value: Any, said: list[tuple[list[Any], str]]
) -> None:
    errors = schema_errors(schema, value)
    assert [(list(error.absolute_path), error.message) for error in errors] == said
    assert patterns._CHECK.get() is None


def back(keyword: str, reference: str) -> str:
    return f"its {keyword} {reference!r} refers back to itself without going into the value"


T = "#/$defs/t"


@pytest.mark.parametrize(
    ("schema", "value", "said"),
    [
        ({"$ref": "#"}, 1, [([], back("$ref", "#"))]),
        ({"$defs": {"t": {"$ref": T}}, "$ref": T}, 1, [([], back("$ref", T))]),
        (
            {"$defs": {"t": {"$ref": "#/$defs/u"}, "u": {"$ref": T}}, "$ref": T},
            1,
            [([], back("$ref", T))],
        ),
        ({"$dynamicAnchor": "t", "$dynamicRef": "#t"}, 1, [([], back("$dynamicRef", "#t"))]),
        (
            {"$id": "https://example.com/t", "$defs": {"t": {"$ref": T}}, "$ref": T},
            1,
            [([], back("$ref", T))],
        ),
        (
            {"properties": {"a": {"$ref": "#/properties/a"}}},
            {"a": 1},
            [(["a"], back("$ref", "#/properties/a"))],
        ),
        # For a string, it's never stopped by its type's error (where a value
        # of any other type is: test_the_keywords_mean_what_jsonschemas_do).
        (
            {"$defs": {"t": {"type": "string", "not": {"$ref": T}}}, "$ref": T},
            "s",
            [([], back("$ref", T))],
        ),
        # Where not fitting it would fit.
        ({"not": {"$ref": "#/not"}}, 1, [([], back("$ref", "#/not"))]),
        ({"anyOf": [{"$ref": "#"}, {}]}, 1, [([], back("$ref", "#"))]),
        ({"if": {"$ref": "#/if"}, "then": False}, 1, [([], back("$ref", "#/if"))]),
        # Where what it evaluates is told, before the value's checked against it or after.
        ({"unevaluatedProperties": False, "$ref": "#"}, {"a": 1}, [([], back("$ref", "#"))]),
        ({"$ref": "#", "unevaluatedProperties": False}, {"a": 1}, [([], back("$ref", "#"))]),
        ({"unevaluatedItems": False, "$ref": "#"}, [1], [([], back("$ref", "#"))]),
        ({"$ref": "#", "unevaluatedItems": False}, [1], [([], back("$ref", "#"))]),
    ],
)
def test_a_reference_that_refers_back_to_itself_refuses_the_value(
    schema: dict[str, Any], value: Any, said: list[tuple[list[Any], str]]
) -> None:
    errors = schema_errors(schema, value)
    assert [(list(error.absolute_path), error.message) for error in errors] == said
    assert patterns._CHECK.get() is None


def nested(depth: int, level: Callable[[Any], Any], inner: Any) -> Any:
    """``inner`` inside ``depth`` levels, each as ``level`` makes it."""
    for _ in range(depth):
        inner = level(inner)
    return inner


TOO_DEEP = "it, or its schema, is nested too deeply to be checked"


@pytest.mark.parametrize(
    ("schema", "value", "said"),
    [
        # A value nested so deep, under a schema that refers to itself.
        (
            {"properties": {"a": {"$ref": "#"}}},
            nested(2000, lambda v: {"a": v}, 1),
            [([], TOO_DEEP)],
        ),
        ({"items": {"$ref": "#"}}, nested(2000, lambda v: [v], 1), [([], TOO_DEEP)]),
        # A schema nested so deep.
        (nested(2000, lambda s: {"allOf": [s]}, {"type": "string"}), 1, [([], TOO_DEEP)]),
        # References going round, through other resources ($id)...
        (
            {
                "$id": "https://example.com/a",
                "$defs": {"b": {"$id": "https://example.com/b", "$ref": "https://example.com/a"}},
                "$ref": "https://example.com/b",
            },
            1,
            [([], TOO_DEEP)],
        ),
        # ...or once what they're followed from has found an error, which each
        # time round they find again: that's not said over and over.
        (
            {"$defs": {"t": {"type": "string", "allOf": [{"$ref": T}]}}, "$ref": T},
            1,
            [([], TOO_DEEP)],
        ),
        # The first pattern or reference that failed it refuses the value.
        (
            {
                "$defs": {"t": {"type": "string", "allOf": [{"$ref": MISSING}, {"$ref": T}]}},
                "$ref": T,
            },
            1,
            [([], refused("$ref", MISSING))],
        ),
    ],
)
def test_a_check_deeper_than_the_stack_refuses_the_value(
    schema: dict[str, Any], value: Any, said: list[tuple[list[Any], str]]
) -> None:
    errors = schema_errors(schema, value)
    assert [(list(error.absolute_path), error.message) for error in errors] == said
    assert patterns._CHECK.get() is None
