"""URLs built so a request to them works: query parameters added from name and
value pairs, each percent-encoded whole, and whatever a URL can't hold as it
is encoded. Standard library only.

    with_query("https://api.example.com/search", [("q", "red & blue"), ("page", "2")])
    # https://api.example.com/search?q=red%20%26%20blue&page=2

HTTP steps and HTTP tools build their URLs with it: their query parameters
are filled in first (templates), then encoded here, so a value with ``&``,
``=``, ``#``, ``/``, spaces or any other character arrives as it was written.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote, urlsplit, urlunsplit

__all__ = ["encoded_url", "query_pairs", "with_query"]

#: What a URL holds as it is: RFC 3986's reserved characters, and escapes
#: already made. (Letters, digits and ``-._~`` are never encoded.)
_URL_SAFE = "!#$%&'()*+,/:;=?@[]"


def encoded_url(url: str) -> str:
    """
    :return: The URL with what it can't hold (spaces, quotes, braces, ``<>``,
        non-ASCII…) percent-encoded, as UTF-8; its reserved characters and
        escapes as they are, so it means what it did.
    """
    return quote(url, safe=_URL_SAFE)


def query_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """
    :return: The pairs as a query (``a=1&b=2``), each name and value
        percent-encoded whole: a space as ``%20``.
    """
    return "&".join(f"{quote(name, safe='')}={quote(value, safe='')}" for name, value in pairs)


def with_query(url: str, pairs: Iterable[tuple[str, str]]) -> str:
    """
    :param url: Where the request goes; a query of its own is kept.
    :param pairs: Query parameters to add, by name and value, as they're
        meant: nothing in them encoded yet. A name may come more than once.
    :return: The URL (:func:`encoded_url`) with the pairs (:func:`query_pairs`)
        after its own query, before its fragment.
    """
    encoded = encoded_url(url)
    added = query_pairs(pairs)
    if not added:
        return encoded
    parts = urlsplit(encoded)
    query = f"{parts.query}&{added}" if parts.query else added
    return urlunsplit(parts._replace(query=query))
