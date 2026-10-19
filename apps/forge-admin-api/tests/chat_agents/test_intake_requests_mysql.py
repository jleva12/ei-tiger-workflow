"""The requests chat agents file to their intakes, kept in MySQL for the
person: read back newest first, by status or ID, only theirs, and gone with
their organization. make admin-test-mysql starts admin-mysql.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from forge_agent_runtime.requests import IntakeRequest as Request
from forge_agent_runtime.services import RequestConflict

from forge_admin.chat_agents.requests import OrganizationRequests
from forge_admin.config import Settings
from forge_admin.db.session import create_engine, create_sessionmaker

pytestmark = pytest.mark.mysql

API = "/api/v1"


def request(intake: str, **changes: Any) -> dict[str, Any]:
    """A request as the runtime saves it."""
    return Request.model_validate(
        {
            "id": str(uuid4()),
            "intake": intake,
            "title": intake.title(),
            "basket": "b1",
            "session_id": "s1",
            "record": {"answers": {"requester_email": {"value": "ada@x.co"}}},
            **changes,
        }
    ).model_dump(mode="json")


def test_requests_are_kept_for_the_person_with_the_agent(
    site_admin: str,
    mysql_settings: Settings,
    client_as: Callable[[str], TestClient],
) -> None:
    admin = client_as(site_admin)
    made = admin.post(f"{API}/organizations", json={"name": f"Desk {uuid4().hex[:8]}"})
    assert made.status_code == 201, made.text
    org = made.json()["id"]
    ada = {"organization_id": org, "agent_id": "ca_desk", "user_id": "ada"}

    async def main() -> None:
        engine = create_engine(mysql_settings)
        store = OrganizationRequests(create_sessionmaker(engine))
        try:
            access, laptop = request("access"), request("laptop")
            await store.save([access], **ada)
            await asyncio.sleep(0.01)
            await store.save([laptop], **ada)
            mine = await store.requests(**ada)
            # Newest first, each whole.
            assert [r["id"] for r in mine] == [laptop["id"], access["id"]]
            assert mine[1]["record"] == access["record"]
            assert mine[1]["intake"] == "access" and mine[1]["basket"] == "b1"
            assert mine[1]["updated_at"].endswith("+00:00")

            # Picked up in another conversation, then submitted: each on the
            # version it was read at, once.
            assert mine[1]["version"] == 0
            picked = {
                **access,
                "session_id": "s2",
                "status": "submitting",
                "version": 0,
            }

            async def submit() -> bool:
                try:
                    await store.save([picked], **ada)
                except RequestConflict:
                    return False
                return True

            assert sum(await asyncio.gather(*(submit() for _ in range(4)))) == 1
            await store.save([{**picked, "status": "submitted", "version": 1}], **ada)
            assert [r["id"] for r in await store.requests(**ada, status="open")] == [
                laptop["id"]
            ]
            (submitted,) = await store.requests(**ada, ids=[access["id"]])
            assert submitted["session_id"] == "s2"
            assert submitted["submitted_at"] is not None
            assert await store.requests(**ada, limit=1) == [submitted]

            # Nobody else's: not another person's, nor another agent's.
            assert await store.requests(**{**ada, "user_id": "bo"}) == []
            assert await store.requests(**{**ada, "agent_id": "ca_other"}) == []
            with pytest.raises(LookupError, match="no request"):
                await store.save([{**laptop, "version": 0}], **{**ada, "user_id": "bo"})
            # Nor a new one under a kept one's ID.
            with pytest.raises(RequestConflict):
                await store.save([laptop], **{**ada, "user_id": "bo"})
            with pytest.raises(ValueError, match="can't be 'lost'"):
                await store.save([{**laptop, "status": "lost"}], **ada)
        finally:
            await engine.dispose()

    asyncio.run(main())

    # They go with their organization.
    assert admin.delete(f"{API}/organizations/{org}").status_code == 204

    async def gone() -> list[dict[str, Any]]:
        engine = create_engine(mysql_settings)
        try:
            return await OrganizationRequests(create_sessionmaker(engine)).requests(
                **ada
            )
        finally:
            await engine.dispose()

    assert asyncio.run(gone()) == []
