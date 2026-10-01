"""Inbound events: endpoint tokens, and checking event types' JSON Schemas
and the payloads sent against them.

Schemas are JSON Schema draft 2020-12 (or the draft their ``$schema``
names), with an object at the root. Formats are checked: ``date``,
``date-time`` and ``time`` as RFC 3339, ``email``, ``uri``, ``uuid``,
``ipv4``, ``ipv6`` and ``regex``.
"""

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, time
from itertools import islice
from typing import Any
from urllib.parse import urlsplit

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.protocols import Validator
from jsonschema.validators import Draft202012Validator, validator_for
from referencing.exceptions import Unresolvable

# What senders name an event type by, in the URL: e.g. incident.opened.
EVENT_KEY_PATTERN = r"^[a-z][a-z0-9_.-]{0,99}$"
# Endpoint tokens start with this, so they're recognizable in a secret scan.
TOKEN_PREFIX = "fevt_"
# The largest schema accepted, serialized.
MAX_SCHEMA_BYTES = 64 * 1024
# The most errors kept for one payload.
MAX_ERRORS = 50
# An error's message, at most.
MAX_MESSAGE = 500

_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_RFC3339_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")

# jsonschema's own checkers, and ours for the formats it would otherwise
# skip without optional packages.
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def _date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not _RFC3339_DATE_TIME.match(value):
        return False
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


@FORMATS.checks("time", raises=ValueError)
def _time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not _RFC3339_TIME.match(value):
        return False
    time.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


@FORMATS.checks("uri", raises=ValueError)
def _uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parts = urlsplit(value)
    return bool(_URI_SCHEME.match(parts.scheme)) and not re.search(r"\s", value)


@dataclass(frozen=True)
class Issue:
    """
    Why a payload doesn't match its schema.

    :param path: Where, as a JSONPath: ``$`` for the payload itself,
        ``$.incident.severity``, ``$.items[0]``.
    :param message: What's wrong.
    :param keyword: The schema keyword it failed, e.g. ``required``.
    """

    path: str
    message: str
    keyword: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Token:
    """
    A new endpoint token. Only its digest is stored; ``value`` is shown
    once.

    :param value: The token, e.g. ``fevt_…``.
    :param sha256: Its SHA-256, hex.
    :param hint: Its last four characters.
    """

    value: str
    sha256: str
    hint: str


def new_token() -> Token:
    """:return: A random endpoint token, 256 bits."""
    value = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return Token(value, token_digest(value), value[-4:])


def token_digest(value: str) -> str:
    """
    :param value: A token as sent.
    :return: Its SHA-256, hex, to compare with the stored one.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def _validator(schema: dict[str, Any]) -> Validator:
    cls = validator_for(schema, default=Draft202012Validator)
    return cls(schema, format_checker=FORMATS)


def _remote_refs(node: object) -> list[str]:
    """:return: The ``$ref`` values in a schema that aren't in it."""
    found: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            found.append(ref)
        for value in node.values():
            found.extend(_remote_refs(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_remote_refs(value))
    return found


def check_schema(schema: object) -> dict[str, Any]:
    """
    Check an event type's payload schema before it's saved.

    :param schema: The schema, as sent.
    :return: The schema.
    :raises ValueError: It isn't a JSON Schema for an object, is too big,
        or refers to schemas outside itself, which are never fetched.
    """
    if not isinstance(schema, dict):
        raise ValueError("The schema must be a JSON object")
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise ValueError(f"The schema is over {MAX_SCHEMA_BYTES // 1024} KiB")
    if schema.get("type") != "object":
        raise ValueError('An event\'s schema describes an object: "type": "object"')
    remote = _remote_refs(schema)
    if remote:
        raise ValueError(
            f"Only references within the schema (#/…) are supported, not {remote[0]}"
        )
    cls = validator_for(schema, default=Draft202012Validator)
    try:
        # With the meta-schema's own formats: a pattern must be a regex.
        cls.check_schema(schema)
    except SchemaError as error:
        where = _json_path(error.absolute_path)
        raise ValueError(
            f"Not a valid JSON Schema at {where}: {_clip(error.message)}"
        ) from None
    return schema


def validate_payload(schema: dict[str, Any], payload: Any) -> list[Issue]:
    """
    Check a payload against its event type's schema.

    :param schema: A schema ``check_schema`` accepted.
    :param payload: The parsed JSON body.
    :return: Up to ``MAX_ERRORS`` issues, in the schema's order; none when
        the payload matches.
    """
    try:
        errors = islice(_validator(schema).iter_errors(payload), MAX_ERRORS)
        return [
            Issue(
                _json_path(error.absolute_path),
                _clip(error.message),
                str(error.validator),
            )
            for error in errors
        ]
    except (Unresolvable, SchemaError) as error:
        # A schema saved before a rule it now breaks; the event is still
        # kept, as not matching.
        return [Issue("$", f"The schema can't be applied: {_clip(str(error))}", "")]


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(part)):
            path += f".{part}"
        else:
            path += f"[{json.dumps(str(part))}]"
    return path


def _clip(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE else message[: MAX_MESSAGE - 1] + "…"
