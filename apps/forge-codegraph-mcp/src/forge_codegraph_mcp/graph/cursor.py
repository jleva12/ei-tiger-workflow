"""Signed pagination cursors, as the worker's store signs them.

A cursor carries the listing's scope (a fingerprint of the database, the
deployment scope and the query's shape), the generation it was read at and
the position to continue after, signed with the shared cursor key. A cursor
from one listing is never accepted by another, and one from an earlier
generation is refused as stale. The encoding is the Go store's (cursor.go),
so cursors cross between this server and the worker's API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from forge_codegraph_mcp.graph.errors import InvalidRequest, StaleGeneration


def go_json(value: Any) -> bytes:
    """
    Encodes a Python object into a JSON-formatted bytes string with additional escaping
    to ensure safety in HTML contexts.

    The function serializes the given value into a JSON string, applies specific character
    escapes for `<`, `>`, `&`, `\u2028`, and `\u2029`, and then converts the resulting
    string into a bytes object with UTF-8 encoding.

    :param value: The data to be serialized into JSON format. Can be any JSON-serializable
        Python object.
    :return: A bytes object representing the JSON-encoded version of the provided value,
        with additional character escaping for safety.
    """
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        encoded = encoded.replace(raw, escaped)
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return encoded.encode()


def _b64(data: bytes) -> str:
    """
    Encodes the given binary data into a URL-safe Base64 encoded string.

    The method takes bytes as input, encodes them using Base64, and ensures
    that the resulting string is URL-safe by replacing characters that might
    conflict in a URL context. Additionally, any trailing '=' padding
    characters from the Base64-encoded string are removed.

    :param data: The binary data to encode.
    :type data: bytes
    :return: A URL-safe Base64-encoded string without padding.
    :rtype: str
    """
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    """
    Decode a Base64 URL-safe encoded string.

    This function decodes a given Base64 URL-safe encoded string into its
    original binary data. It ensures padding is correctly added based on
    the Base64 encoding rules before performing the decoding.

    :param text: The Base64 URL-safe encoded string to decode.
    :type text: str
    :return: The original binary data decoded from the input string.
    :rtype: bytes
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CursorCodec:
    """
    Encodes and decodes pagination cursors with cryptographic signatures.

    This class is designed to handle the secure generation and validation of
    pagination cursors used in applications. It ensures that the cursors
    cannot be tampered with and are uniquely associated with a specific
    scope and listing generation. The primary use case for this class is to
    assist in implementing safe, signed pagination mechanisms.

    :ivar key: The cryptographic key used for HMAC signing.
    :type key: bytes
    :ivar domain: The domain or namespace associated with the listing.
    :type domain: str
    :ivar max_bytes: The maximum allowed length for cursor tokens.
    :type max_bytes: int
    """

    def __init__(self, key: bytes, domain: str, max_bytes: int = 8 << 10) -> None:
        self._key = key
        self._domain = domain
        self._max_bytes = max_bytes

    def scope(self, *parts: Any) -> str:
        """The fingerprint of a listing: the database and scope, then the
        query's shape."""
        return hashlib.sha256(go_json([self._domain, *parts])).hexdigest()

    def encode(self, scope: str, generation: int, position: str) -> str:
        """Signs a position; an empty position means there is no next page."""
        if not position:
            return ""
        body = go_json({"s": scope, "g": generation, "p": position})
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return _b64(body) + "." + _b64(signature)

    def decode(self, scope: str, generation: int, token: str) -> str:
        """The position of a token for this listing and generation; an empty
        token is the first page."""
        if not token:
            return ""
        if len(token) > self._max_bytes:
            raise InvalidRequest("cursor too long")
        body_text, dot, signature_text = token.partition(".")
        if not dot:
            raise InvalidRequest("malformed cursor")
        try:
            body, signature = _unb64(body_text), _unb64(signature_text)
        except ValueError:
            raise InvalidRequest("malformed cursor") from None
        expected = hmac.new(self._key, body, hashlib.sha256).digest()
        try:
            cursor = json.loads(body) if hmac.compare_digest(signature, expected) else None
        except ValueError:
            cursor = None
        if (
            not isinstance(cursor, dict)
            or cursor.get("s") != scope
            or not isinstance(cursor.get("p"), str)
            or not cursor["p"]
        ):
            raise InvalidRequest("cursor signature or scope")
        if cursor.get("g") != generation:
            raise StaleGeneration(f"cursor generation {cursor.get('g')}, current {generation}")
        return str(cursor["p"])
