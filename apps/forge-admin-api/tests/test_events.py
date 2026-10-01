"""Checking event types' schemas and the payloads sent against them, and
endpoint tokens."""

import pytest

from forge_admin.events import (
    MAX_ERRORS,
    TOKEN_PREFIX,
    Issue,
    check_schema,
    new_token,
    token_digest,
    validate_payload,
)

INCIDENT = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "severity": {"type": "string", "enum": ["sev1", "sev2", "sev3"]},
        "opened_at": {"type": "string", "format": "date-time"},
        "service": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "runbook": {"type": ["string", "null"], "format": "uri"},
            },
            "required": ["name"],
        },
        "responders": {
            "type": "array",
            "items": {"type": "string", "format": "email"},
            "uniqueItems": True,
        },
        "impact": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["id", "severity", "opened_at"],
}

VALID = {
    "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
    "severity": "sev2",
    "opened_at": "2026-09-26T08:15:00Z",
    "service": {"name": "checkout", "runbook": None},
    "responders": ["ada@example.com"],
    "impact": 40,
}


def test_a_matching_payload_has_no_issues() -> None:
    assert validate_payload(check_schema(INCIDENT), VALID) == []


def test_issues_say_where_and_why_by_keyword() -> None:
    payload = {
        **VALID,
        "severity": "sev9",
        "opened_at": "yesterday",
        "service": {"runbook": "not a uri"},
        "responders": ["ada@example.com", "ada@example.com"],
        "impact": 101,
    }
    del payload["id"]
    issues = validate_payload(INCIDENT, payload)
    assert {(issue.path, issue.keyword) for issue in issues} == {
        ("$", "required"),
        ("$.severity", "enum"),
        ("$.opened_at", "format"),
        ("$.service", "required"),
        ("$.service.runbook", "format"),
        ("$.responders", "uniqueItems"),
        ("$.impact", "maximum"),
    }
    assert Issue("$", "'id' is a required property", "required") in issues


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("2026-09-26T08:15:00Z", True),
        ("2026-09-26T08:15:00.123+02:00", True),
        ("2026-09-26 08:15:00z", True),
        # RFC 3339 needs the offset, and a real date.
        ("2026-09-26T08:15:00", False),
        ("2026-02-30T08:15:00Z", False),
    ],
)
def test_date_times_are_rfc_3339(value: str, valid: bool) -> None:
    schema = {"type": "object", "properties": {"at": {"format": "date-time"}}}
    assert (validate_payload(schema, {"at": value}) == []) is valid


def test_paths_quote_keys_that_arent_names() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"a-b": {"type": "string"}}},
            }
        },
    }
    [issue] = validate_payload(schema, {"items": [{"a-b": "x"}, {"a-b": 1}]})
    assert issue.path == '$.items[1]["a-b"]'


def test_issues_stop_at_the_limit() -> None:
    schema = {"type": "object", "additionalProperties": {"type": "string"}}
    payload = {f"k{i}": i for i in range(MAX_ERRORS + 10)}
    assert len(validate_payload(schema, payload)) == MAX_ERRORS


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ([], "must be a JSON object"),
        ({"type": "string"}, '"type": "object"'),
        ({"properties": {}}, '"type": "object"'),
        (
            {"type": "object", "properties": {"a": {"type": "text"}}},
            r"at \$\.properties\.a\.type",
        ),
        (
            {"type": "object", "properties": {"a": {"pattern": "("}}},
            "is not a 'regex'",
        ),
        (
            {"type": "object", "properties": {"a": {"$ref": "https://example.com/a"}}},
            "Only references within the schema",
        ),
        (
            {"type": "object", "description": "x" * 70_000},
            "over 64 KiB",
        ),
    ],
)
def test_schemas_an_event_type_cant_use(schema: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        check_schema(schema)


def test_local_references_and_older_drafts_work() -> None:
    schema = check_schema(
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "definitions": {"name": {"type": "string", "minLength": 2}},
            "properties": {"owner": {"$ref": "#/definitions/name"}},
        }
    )
    [issue] = validate_payload(schema, {"owner": "a"})
    assert (issue.path, issue.keyword) == ("$.owner", "minLength")


def test_tokens_are_random_and_only_their_digest_is_kept() -> None:
    first, second = new_token(), new_token()
    assert first.value != second.value
    assert first.value.startswith(TOKEN_PREFIX)
    assert len(first.value) > 40
    assert first.sha256 == token_digest(first.value)
    assert first.value not in first.sha256
    assert first.hint == first.value[-4:]
