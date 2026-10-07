"""Cursors are signed and fingerprinted byte for byte as the worker's Go store
does (the expected values come from it), so they cross between the two."""

import pytest

from forge_codegraph_mcp.graph.cursor import CursorCodec
from forge_codegraph_mcp.graph.errors import InvalidRequest, StaleGeneration

DOMAIN = "projects/p/instances/i/databases/d|codegraph"
KEY = b"0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    ("parts", "fingerprint"),
    [
        (
            ("neighbors", "repo:x", "method:abc", "both", ["calls", "references"]),
            "79b0ae2e68bd40ec1ff668e91470d91cde6d81ae61cf60792aa3469adb26f4d9",
        ),
        (
            ("neighbors", "repo:x", "method:abc", "in", None),
            "eaa6f6213041f9bdaca121c26693d1ca6216167cb133688ef1ad5bc4bbef68ed",
        ),
        (
            ("changes", "repo:x", 7, "node"),
            "556da7f8c1933b2352fbe51502eb4bb896580bac24cb1513454ad07e68eb0fb5",
        ),
        # Go escapes <, > and & and writes other characters as UTF-8.
        (
            ("list", "a<b>&c", "é"),
            "ddfac3b9e40288ea2d8dcfe3fa974a8e87816e44a03bbdc20d45c3069260b1da",
        ),
    ],
)
def test_scope_fingerprints_match_go(parts: tuple[object, ...], fingerprint: str) -> None:
    assert CursorCodec(KEY, DOMAIN).scope(*parts) == fingerprint


def test_encoding_matches_go() -> None:
    token = CursorCodec(KEY, DOMAIN).encode("abc", 7, "method:xyz")
    assert token == (
        "eyJzIjoiYWJjIiwiZyI6NywicCI6Im1ldGhvZDp4eXoifQ.BVxVI9B6ruyiLqnGDq5SUtRJzoZnhctB7V5LD75NfRw"
    )


def test_round_trip_and_refusals() -> None:
    codec = CursorCodec(KEY, DOMAIN)
    scope = codec.scope("neighbors", "repo:x", "method:abc", "both", None)
    token = codec.encode(scope, 3, "edge:after")
    assert codec.decode(scope, 3, token) == "edge:after"
    assert codec.decode(scope, 3, "") == ""
    assert codec.encode(scope, 3, "") == ""
    with pytest.raises(StaleGeneration):
        codec.decode(scope, 4, token)
    with pytest.raises(InvalidRequest, match="signature or scope"):
        codec.decode(codec.scope("other"), 3, token)
    with pytest.raises(InvalidRequest, match="signature or scope"):
        CursorCodec(b"x" * 32, DOMAIN).decode(scope, 3, token)
    with pytest.raises(InvalidRequest, match="malformed"):
        codec.decode(scope, 3, "no-dot")
    with pytest.raises(InvalidRequest, match="too long"):
        codec.decode(scope, 3, "x" * 9000)
