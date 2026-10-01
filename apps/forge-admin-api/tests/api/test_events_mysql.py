"""Inbound events, against MySQL: an organization admin turns on the
organization's endpoint and defines event types with their schemas; a sender
posts events with the endpoint's token; the organization reads what arrived.

A site administrator builds an organization with an admin and a member.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.mysql

API = "/api/v1"

INCIDENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "severity": {"type": "string", "enum": ["sev1", "sev2", "sev3"]},
        "opened_at": {"type": "string", "format": "date-time"},
        "service": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
        },
    },
    "required": ["id", "severity"],
}
INCIDENT = {
    "id": "INC-1042",
    "severity": "sev2",
    "opened_at": "2026-09-26T08:15:00Z",
    "service": {"name": "checkout"},
}


@dataclass
class Organization:
    id: str
    admin_id: str
    admin: TestClient
    member: TestClient
    outsider: TestClient
    # No user at all, as an outside system calls.
    sender: TestClient

    @property
    def endpoint(self) -> str:
        return f"{API}/organizations/{self.id}/event-endpoint"

    @property
    def event_types(self) -> str:
        return f"{API}/organizations/{self.id}/event-types"

    @property
    def events(self) -> str:
        return f"{API}/organizations/{self.id}/events"


@pytest.fixture
def organization(
    site_admin: str, client_as: Callable[[str], TestClient]
) -> Iterator[Organization]:
    admin = client_as(site_admin)
    tag = uuid4().hex[:8]
    organization_id = admin.post(
        f"{API}/organizations", json={"name": f"Org {tag}"}
    ).json()["id"]
    org_admin, member = f"admin-{tag}", f"member-{tag}"
    for user, role in ((org_admin, "org:admin"), (member, "org:member")):
        assigned = admin.put(
            f"{API}/scopes/org:{organization_id}/members/{user}/roles/{role}"
        )
        assert assigned.status_code == 200

    yield Organization(
        organization_id,
        org_admin,
        client_as(org_admin),
        client_as(member),
        client_as(f"outsider-{tag}"),
        client_as(f"sender-{tag}"),
    )

    # Deleting the organization drops its endpoint, event types and events.
    admin.delete(f"{API}/organizations/{organization_id}")


@dataclass
class Endpoint:
    url: str
    token: str


def turn_on(organization: Organization) -> Endpoint:
    made = organization.admin.put(organization.endpoint, json={"enabled": True})
    assert made.status_code == 200, made.json()
    return Endpoint(made.json()["url"], made.json()["token"])


def define(
    organization: Organization,
    key: str = "incident.opened",
    active: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """Define an event type, and turn it on unless it should stay a draft."""
    created = organization.admin.post(
        organization.event_types,
        json={
            "key": key,
            "name": "Incident opened",
            "payload_schema": INCIDENT_SCHEMA,
            **fields,
        },
    )
    assert created.status_code == 201, created.json()
    assert created.json()["status"] == "draft"
    if not active:
        return created.json()
    turned_on = organization.admin.patch(
        f"{organization.event_types}/{created.json()['id']}", json={"status": "active"}
    )
    assert turned_on.status_code == 200, turned_on.json()
    return turned_on.json()


def send(
    organization: Organization,
    endpoint: Endpoint,
    payload: Any,
    key: str = "incident.opened",
    token: str | None = None,
    **headers: str,
) -> Any:
    path = endpoint.url.removeprefix("http://testserver")
    return organization.sender.post(
        f"{path}/{key}",
        json=payload,
        headers={"Authorization": f"Bearer {token or endpoint.token}", **headers},
    )


def test_an_admin_turns_the_endpoint_on_and_gets_its_token_once(
    organization: Organization,
) -> None:
    assert organization.member.get(organization.endpoint).json() is None
    endpoint = turn_on(organization)
    assert endpoint.token.startswith("fevt_")

    shown = organization.member.get(organization.endpoint).json()
    assert shown["enabled"] is True
    assert shown["token"] is None
    assert shown["token_hint"] == endpoint.token[-4:]
    assert shown["url"] == f"http://testserver/hooks/events/{shown['id']}"
    assert shown["created_by"] == organization.admin_id

    # Turning it off and on again keeps the token.
    assert (
        organization.admin.put(organization.endpoint, json={"enabled": False}).json()[
            "token"
        ]
        is None
    )
    again = organization.admin.put(organization.endpoint, json={"enabled": True}).json()
    assert (again["id"], again["token"]) == (shown["id"], None)


def test_only_those_who_manage_events_change_them(organization: Organization) -> None:
    for refused in (
        organization.member.put(organization.endpoint, json={"enabled": True}),
        organization.member.post(f"{organization.endpoint}/token"),
        organization.member.post(
            organization.event_types,
            json={"key": "a", "name": "A", "payload_schema": {"type": "object"}},
        ),
    ):
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Requires events:manage"
    assert organization.outsider.get(organization.event_types).status_code == 403
    assert organization.outsider.get(organization.events).status_code == 403


def test_event_types_need_a_usable_schema_and_a_free_key(
    organization: Organization,
) -> None:
    created = define(organization)
    assert (created["schema_version"], created["event_count"]) == (1, 0)
    assert created["payload_schema"] == INCIDENT_SCHEMA

    taken = organization.admin.post(
        organization.event_types,
        json={
            "key": "incident.opened",
            "name": "Again",
            "payload_schema": {"type": "object"},
        },
    )
    assert taken.status_code == 409
    not_object = organization.admin.post(
        organization.event_types,
        json={"key": "b", "name": "B", "payload_schema": {"type": "array"}},
    )
    assert not_object.status_code == 422
    assert '"type": "object"' in not_object.json()["detail"]
    bad_key = organization.admin.post(
        organization.event_types,
        json={
            "key": "Incident Opened",
            "name": "C",
            "payload_schema": {"type": "object"},
        },
    )
    assert bad_key.status_code == 422


def test_senders_post_events_and_the_organization_reads_them(
    organization: Organization,
) -> None:
    endpoint = turn_on(organization)
    event_type = define(organization)

    valid = send(organization, endpoint, INCIDENT, **{"User-Agent": "pager/1.0"})
    assert valid.status_code == 202, valid.json()
    assert valid.json()["status"] == "valid"
    assert valid.json()["event_type"] == "incident.opened"
    assert valid.json()["schema_version"] == 1

    invalid = send(
        organization, endpoint, {**INCIDENT, "severity": "sev9", "service": {}}
    )
    assert invalid.status_code == 422
    assert invalid.json()["status"] == "invalid"
    assert [(e["path"], e["keyword"]) for e in invalid.json()["errors"]] == [
        ("$.severity", "enum"),
        ("$.service", "required"),
    ]

    listed = organization.member.get(organization.events).json()
    assert [e["id"] for e in listed] == [invalid.json()["id"], valid.json()["id"]]
    assert "payload" not in listed[0]
    assert listed[0]["event_key"] == "incident.opened"
    assert listed[1]["user_agent"] == "pager/1.0"
    assert listed[1]["created_by"].startswith("endpoint:")
    only_invalid = organization.member.get(
        organization.events, params={"status": "invalid"}
    ).json()
    assert [e["id"] for e in only_invalid] == [invalid.json()["id"]]

    read = organization.member.get(f"{organization.events}/{valid.json()['id']}").json()
    assert read["payload"] == INCIDENT
    assert read["errors"] == []

    counted = organization.member.get(
        f"{organization.event_types}/{event_type['id']}"
    ).json()
    assert (counted["event_count"], counted["invalid_count"]) == (2, 1)
    assert counted["last_received_at"] is not None


def test_an_idempotency_key_makes_retries_safe(organization: Organization) -> None:
    endpoint = turn_on(organization)
    define(organization)
    first = send(organization, endpoint, INCIDENT, **{"Idempotency-Key": "INC-1042"})
    retry = send(organization, endpoint, INCIDENT, **{"Idempotency-Key": "INC-1042"})
    assert (first.status_code, retry.status_code) == (202, 200)
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["duplicate"] is True
    assert len(organization.member.get(organization.events).json()) == 1


def test_the_endpoint_refuses_what_it_cant_take(organization: Organization) -> None:
    endpoint = turn_on(organization)
    define(organization)
    paused = define(organization, key="defect.found")
    organization.admin.patch(
        f"{organization.event_types}/{paused['id']}", json={"status": "paused"}
    )
    path = endpoint.url.removeprefix("http://testserver")

    assert (
        organization.sender.post(f"{path}/incident.opened", json=INCIDENT).status_code
        == 401
    )
    assert send(organization, endpoint, INCIDENT, token="fevt_wrong").status_code == 401
    unknown = f"/hooks/events/{uuid4()}/incident.opened"
    assert (
        organization.sender.post(
            unknown,
            json=INCIDENT,
            headers={"Authorization": f"Bearer {endpoint.token}"},
        ).status_code
        == 401
    )
    # For senders that can't set Authorization.
    header = organization.sender.post(
        f"{path}/incident.opened",
        json=INCIDENT,
        headers={"X-Forge-Token": endpoint.token},
    )
    assert header.status_code == 202

    assert send(organization, endpoint, INCIDENT, key="deploy.done").status_code == 404
    refused = send(organization, endpoint, INCIDENT, key="defect.found")
    assert (refused.status_code, refused.json()["detail"]) == (
        409,
        "Event type defect.found is paused",
    )
    not_json = organization.sender.post(
        f"{path}/incident.opened",
        content=b"{not json",
        headers={"Authorization": f"Bearer {endpoint.token}"},
    )
    assert not_json.status_code == 400
    too_large = send(organization, endpoint, {"blob": "x" * 300_000})
    assert too_large.status_code == 413

    organization.admin.put(organization.endpoint, json={"enabled": False})
    off = send(organization, endpoint, INCIDENT)
    assert (off.status_code, off.json()["detail"]) == (
        403,
        "The endpoint is turned off",
    )
    # Only the one accepted event was kept.
    assert len(organization.member.get(organization.events).json()) == 1


def test_rotating_the_token_retires_the_old_one(organization: Organization) -> None:
    endpoint = turn_on(organization)
    define(organization)
    rotated = organization.admin.post(f"{organization.endpoint}/token").json()
    assert rotated["token"] != endpoint.token
    assert send(organization, endpoint, INCIDENT).status_code == 401
    assert (
        send(organization, endpoint, INCIDENT, token=rotated["token"]).status_code
        == 202
    )


def test_changing_the_schema_bumps_its_version(organization: Organization) -> None:
    endpoint = turn_on(organization)
    event_type = define(organization)
    url = f"{organization.event_types}/{event_type['id']}"
    send(organization, endpoint, INCIDENT)

    renamed = organization.admin.patch(
        url, json={"name": "Incident", "payload_schema": INCIDENT_SCHEMA}
    )
    assert (renamed.json()["name"], renamed.json()["schema_version"]) == ("Incident", 1)
    stricter = {
        **INCIDENT_SCHEMA,
        "required": ["id", "severity", "opened_at", "service"],
    }
    changed = organization.admin.patch(url, json={"payload_schema": stricter})
    assert changed.json()["schema_version"] == 2
    assert (
        organization.admin.patch(
            url, json={"payload_schema": {"type": "x"}}
        ).status_code
        == 422
    )

    later = send(organization, endpoint, {"id": "INC-7", "severity": "sev1"})
    assert later.status_code == 422
    assert later.json()["schema_version"] == 2
    versions = [
        e["schema_version"] for e in organization.member.get(organization.events).json()
    ]
    assert versions == [2, 1]


def test_trying_a_payload_stores_nothing(organization: Organization) -> None:
    trial = f"{API}/organizations/{organization.id}/event-schemas/validate"
    ok = organization.member.post(
        trial, json={"payload_schema": INCIDENT_SCHEMA, "payload": INCIDENT}
    )
    assert ok.json() == {"valid": True, "errors": []}
    bad = organization.member.post(
        trial, json={"payload_schema": INCIDENT_SCHEMA, "payload": {"id": 5}}
    ).json()
    assert bad["valid"] is False
    assert {e["path"] for e in bad["errors"]} == {"$", "$.id"}
    unusable = organization.member.post(
        trial, json={"payload_schema": {"type": 1}, "payload": {}}
    )
    assert unusable.status_code == 422
    assert organization.member.get(organization.events).json() == []


def test_deleting_an_event_type_deletes_its_events(organization: Organization) -> None:
    endpoint = turn_on(organization)
    event_type = define(organization)
    send(organization, endpoint, INCIDENT)
    deleted = organization.admin.delete(
        f"{organization.event_types}/{event_type['id']}"
    )
    assert deleted.status_code == 204
    assert organization.member.get(organization.events).json() == []
    assert send(organization, endpoint, INCIDENT).status_code == 404


def test_schemas_and_payloads_keep_their_order(organization: Organization) -> None:
    endpoint = turn_on(organization)
    properties = {"zeta": {"type": "string"}, "alpha": {"type": "integer"}}
    event_type = define(
        organization, payload_schema={"type": "object", "properties": properties}
    )
    url = f"{organization.event_types}/{event_type['id']}"
    assert list(
        organization.member.get(url).json()["payload_schema"]["properties"]
    ) == [
        "zeta",
        "alpha",
    ]
    # Reordering is saved, but checks the same: still version 1.
    reordered = {"type": "object", "properties": dict(reversed(properties.items()))}
    saved = organization.admin.patch(url, json={"payload_schema": reordered}).json()
    assert list(saved["payload_schema"]["properties"]) == ["alpha", "zeta"]
    assert saved["schema_version"] == 1

    sent = send(organization, endpoint, {"zeta": "z", "alpha": 1, "middle": None})
    read = organization.member.get(f"{organization.events}/{sent.json()['id']}").json()
    assert list(read["payload"]) == ["zeta", "alpha", "middle"]


def test_a_new_event_type_is_a_draft_until_it_is_turned_on(
    organization: Organization,
) -> None:
    endpoint = turn_on(organization)
    draft = define(organization, active=False)
    url = f"{organization.event_types}/{draft['id']}"

    refused = send(organization, endpoint, INCIDENT)
    assert refused.status_code == 409
    assert "is a draft" in refused.json()["detail"]
    assert organization.member.get(organization.events).json() == []

    # A member reads it but doesn't turn it on; nothing turns it back into a
    # draft.
    assert organization.member.patch(url, json={"status": "active"}).status_code == 403
    assert organization.admin.patch(url, json={"status": "draft"}).status_code == 422
    assert (
        organization.admin.patch(url, json={"status": "active"}).json()["status"]
        == "active"
    )
    assert send(organization, endpoint, INCIDENT).status_code == 202
    paused = organization.admin.patch(url, json={"status": "paused"}).json()
    assert paused["status"] == "paused"
    assert send(organization, endpoint, INCIDENT).status_code == 409
