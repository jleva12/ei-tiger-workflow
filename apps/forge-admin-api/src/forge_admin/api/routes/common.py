"""Request and response pieces shared by the routes."""

from typing import Annotated

from fastapi import Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.auth.access import UUID_PATTERN
from forge_admin.db.audit import UtcDateTime
from forge_admin.db.session import get_session
from forge_admin.models import Organization, User

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
    """:return: A user's name, or their email, or their ID when unknown."""
    person = await session.get(User, user)
    if person is None:
        return user
    return f"{person.first_name} {person.last_name}".strip() or person.email
