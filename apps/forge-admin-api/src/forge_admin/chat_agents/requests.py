"""The requests chat agents file to their intakes, kept in the admin
database: the hosted runtime's ``forge_agent_runtime.services.RequestStore``.

An agent with intake agents on its Tools way files the person's requests to
them itself (``forge_agent_runtime.requests``). Each is kept for the person
with the agent, in its organization, so a conversation can pick up one left
open in another.

Each change is kept only on the version it was read at (compare-and-set, a
conditional update of ``version``), all the changes of one save or none: two
changes made at once (parallel calls, two tabs) can't undo each other, and a
request goes from ``open`` to ``submitting`` once.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from forge_agent_runtime.services import RequestConflict
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.db.audit import utc_now
from forge_admin.models import IntakeRequest

#: What a request's status may be.
STATUSES = frozenset({"open", "submitting", "submitted", "cancelled"})


def _iso(value: datetime | None) -> str | None:
    # Stored naive, in UTC.
    return None if value is None else f"{value.isoformat()}+00:00"


def request_of(row: IntakeRequest) -> dict[str, Any]:
    """:return: A request as the runtime reads it (``IntakeRequest``'s fields)."""
    return {
        "id": row.id,
        "intake": row.intake,
        "title": row.title,
        "basket": row.basket,
        "session_id": row.session_id,
        "status": row.status,
        "record": row.record,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "submitted_at": _iso(row.submitted_at),
        "version": row.version,
    }


class OrganizationRequests:
    """The hosted runtime's requests: each the organization's, the agent's and the person's."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def requests(
        self,
        *,
        organization_id: str | None,
        agent_id: str,
        user_id: str,
        ids: Sequence[str] | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if organization_id is None:
            return []
        query = select(IntakeRequest).where(
            IntakeRequest.organization_id == organization_id,
            IntakeRequest.agent_id == agent_id,
            IntakeRequest.user_id == user_id,
        )
        if ids is not None:
            query = query.where(IntakeRequest.id.in_(list(ids)))
        if status is not None:
            query = query.where(IntakeRequest.status == status)
        query = query.order_by(IntakeRequest.updated_at.desc(), IntakeRequest.id)
        if limit is not None:
            query = query.limit(limit)
        async with self.sessions() as session:
            return [request_of(row) for row in await session.scalars(query)]

    async def save(
        self,
        requests: Sequence[Mapping[str, Any]],
        *,
        organization_id: str | None,
        agent_id: str,
        user_id: str,
    ) -> None:
        if not requests:
            return
        if organization_id is None:
            raise LookupError(
                "The agent belongs to no organization: its requests can't be kept."
            )
        owner = (organization_id, agent_id, user_id)
        async with self.sessions() as session:
            try:
                for request in requests:
                    await self._keep(session, request, owner)
                await session.commit()
            except IntegrityError:
                # A new one's ID is taken.
                await session.rollback()
                raise RequestConflict(
                    "A request with that ID is kept already: read it again."
                ) from None
            except BaseException:
                await session.rollback()
                raise

    async def _keep(
        self,
        session: AsyncSession,
        request: Mapping[str, Any],
        owner: tuple[str, str, str],
    ) -> None:
        """Adds a new request, or changes one on the version it was read at."""
        organization_id, agent_id, user_id = owner
        status = str(request.get("status") or "open")
        if status not in STATUSES:
            raise ValueError(f"A request can't be {status!r}.")
        values: dict[str, Any] = {
            "session_id": str(request.get("session_id") or ""),
            "title": str(request.get("title") or "")[:255],
            "record": dict(request.get("record") or {}),
            "status": status,
        }
        version = request.get("version")
        if version is None:
            session.add(
                IntakeRequest(
                    id=str(request["id"]),
                    organization_id=organization_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    intake=str(request["intake"]),
                    basket=str(request.get("basket") or request["id"]),
                    submitted_at=utc_now() if status == "submitted" else None,
                    version=0,
                    **values,
                )
            )
            return
        if status == "submitted":
            values["submitted_at"] = func.coalesce(
                IntakeRequest.submitted_at, utc_now()
            )
        changed = await session.execute(
            update(IntakeRequest)
            .where(
                IntakeRequest.id == str(request["id"]),
                IntakeRequest.organization_id == organization_id,
                IntakeRequest.agent_id == agent_id,
                IntakeRequest.user_id == user_id,
                IntakeRequest.version == int(version),
            )
            .values(**values, version=int(version) + 1)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:  # type: ignore[attr-defined]
            await self._refuse(str(request["id"]), owner)

    async def _refuse(self, request_id: str, owner: tuple[str, str, str]) -> None:
        """
        :raises LookupError: The request isn't the person's (an ID the model
            made up, or was given).
        :raises RequestConflict: It changed since it was read.
        """
        async with self.sessions() as session:
            row = await session.get(IntakeRequest, request_id)
        if row is None or (row.organization_id, row.agent_id, row.user_id) != owner:
            raise LookupError(f"There's no request {request_id} of theirs.")
        raise RequestConflict(f"Request {request_id} changed since it was read.")
