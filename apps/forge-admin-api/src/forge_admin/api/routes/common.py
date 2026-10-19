"""Request and response pieces shared by the routes."""

from typing import Annotated

from fastapi import Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.access import UUID_PATTERN
from forge_admin.auth.api_keys import key_id_of
from forge_admin.db.audit import UtcDateTime
from forge_admin.db.session import get_session
from forge_admin.models import AgentPermission, ApiKey, Organization, User

Session = Annotated[AsyncSession, Depends(get_session)]
Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
NodeId = Annotated[str, Path(pattern=rf"^{UUID_PATTERN}$")]


class NodeCreate(BaseModel):
    name: Name
    description: str = ""


class NodeUpdate(BaseModel):
    name: Name | None = None
    description: str | None = None


class Audited(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    created_at: UtcDateTime
    created_by: str
    updated_at: UtcDateTime
    updated_by: str


def as_read[ReadModel: BaseModel](
    model: type[ReadModel], record: object, **extra: object
) -> ReadModel:
    """
    Build a response model from a record's attributes plus extra values.

    :param model: The response model.
    :param record: The ORM record.
    :param extra: Values the record does not have, such as its Casbin domain.
    :return: The response.
    """
    fields = {
        name: getattr(record, name) for name in model.model_fields if name not in extra
    }
    return model.model_validate({**fields, **extra})


def apply_update(node: Organization, body: NodeUpdate) -> None:
    """
    Copy the fields a PATCH request sent onto a node of the hierarchy.

    :param node: The record to change.
    :param body: The request body; absent fields are left alone.
    """
    if body.name is not None:
        node.name = body.name
    if body.description is not None:
        node.description = body.description


class AccessRead(BaseModel):
    """
    Which of the organization's agent permissions let someone use a chat
    agent or a workflow (``forge_admin.chat_agents.access``).
    """

    #: Any one of them does; none for anyone who may call the organization's agents.
    permission_ids: list[int]
    updated_at: UtcDateTime
    updated_by: str
    updated_by_name: str | None


class AccessChange(BaseModel):
    """Which of the organization's agent permissions let someone use it."""

    #: Any one of them does; empty for anyone who may call the organization's agents.
    permission_ids: list[int] = Field(max_length=50)


async def checked_permission_ids(
    session: AsyncSession, organization_id: str, ids: list[int]
) -> list[int]:
    """
    :return: The IDs, each once, in order.
    :raises HTTPException: 422 for one that isn't one of the organization's
        agent permissions.
    """
    wanted = list(dict.fromkeys(ids))
    known = set(
        await session.scalars(
            select(AgentPermission.id).where(
                AgentPermission.organization_id == organization_id,
                AgentPermission.id.in_(wanted),
            )
        )
    )
    unknown = [i for i in wanted if i not in known]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown agent permission IDs: {unknown}",
        )
    return wanted


async def commit_or_conflict(session: AsyncSession, detail: str) -> None:
    """
    Commit, turning a uniqueness violation into 409.

    :param session: The request's database session.
    :param detail: The conflict message, e.g. that the name is taken.
    :raises HTTPException: 409 on a duplicate.
    """
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from None


async def name_of(session: AsyncSession, user: str) -> str:
    """
    :return: A user's name, or their email, or their ID when unknown; for an
        organization's API key (``apikey:<id>``), "API key" and its name.
    """
    key_id = key_id_of(user)
    if key_id is not None:
        key = await session.get(ApiKey, key_id)
        return f"API key {key.name}" if key is not None else "Deleted API key"
    person = await session.get(User, user)
    if person is None:
        return user
    return f"{person.first_name} {person.last_name}".strip() or person.email
