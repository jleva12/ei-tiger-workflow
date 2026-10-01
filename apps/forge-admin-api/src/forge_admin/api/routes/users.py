"""The people who use Forge, to pick from when assigning roles.

Anyone signed in can list them; adding, editing and removing them requires
the users:* permissions on the site.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import delete, select

from forge_admin.api.routes.common import Audited, Session, commit_or_conflict
from forge_admin.auth.access import SITE, CurrentUser, Enforcer, authorize, current_user
from forge_admin.auth.authorization import ROLE_KEY_PATTERN, SUBJECT_PATTERN
from forge_admin.models import CasbinRule, User

router = APIRouter(prefix="/users", tags=["users"])

EMAIL_TAKEN = "A user with this email exists"
MSID_TAKEN = "A user with this MS ID exists"
# Both are checked first; this covers a race between the check and the commit.
TAKEN = "A user with this email or MS ID exists"

UserId = Annotated[
    str, StringConstraints(pattern=SUBJECT_PATTERN), Field(description="Subject ID")
]
UserIdPath = Annotated[str, Path(pattern=SUBJECT_PATTERN, description="Subject ID")]
# Deliberately loose: one @ and no spaces. Stored in lowercase.
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+$",
    ),
    Field(examples=["ada@example.com"]),
]
# A network user ID: letters, digits, ".", "_" and "-". Stored in lowercase;
# the pattern is checked before lowercasing, so it allows either case.
Msid = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
    Field(examples=["alovelace"]),
]
PersonName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class UserCreate(BaseModel):
    # Leave it out for a random UUID; give one to match an existing subject.
    id: UserId | None = None
    first_name: PersonName
    last_name: PersonName
    email: Email
    msid: Msid


class UserUpdate(BaseModel):
    first_name: PersonName | None = None
    last_name: PersonName | None = None
    email: Email | None = None
    msid: Msid | None = None


class UserRead(Audited):
    id: str
    first_name: str
    last_name: str
    email: str
    msid: str


async def _get(session: Session, user_id: str) -> User:
    record = await session.get(User, user_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return record


async def _require_unique(
    session: Session, *, email: str | None, msid: str | None, other_than: str = ""
) -> None:
    """Refuse an email or MS ID another user has, saying which one (409)."""
    for column, value, detail in (
        (User.email, email, EMAIL_TAKEN),
        (User.msid, msid, MSID_TAKEN),
    ):
        if value is not None and await session.scalar(
            select(User.id).where(column == value, User.id != other_than).limit(1)
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, detail)


@router.get("", dependencies=[Depends(current_user)])
async def list_users(session: Session) -> list[UserRead]:
    """
    List everyone who uses Forge.
    \f
    :param session: The request's database session.
    :return: Users ordered by first name, last name, then email.
    """
    result = await session.scalars(
        select(User).order_by(User.first_name, User.last_name, User.email)
    )
    return [UserRead.model_validate(u) for u in result]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, user: CurrentUser, session: Session, enforcer: Enforcer
) -> UserRead:
    """
    Add a person, so roles can be assigned to them.
    \f
    :param body: Their names, email, MS ID and, optionally, subject ID.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The new user.
    """
    await authorize(session, enforcer, user, "users:create", SITE)
    if body.id is not None:
        # Roles and subjects share Casbin's namespace; see assign_role.
        if re.fullmatch(ROLE_KEY_PATTERN, body.id):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "User IDs cannot look like role keys",
            )
        if await session.get(User, body.id) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "A user with this ID exists")
    await _require_unique(session, email=body.email, msid=body.msid)
    record = User(**body.model_dump(exclude_none=True))
    session.add(record)
    await commit_or_conflict(session, TAKEN)
    return UserRead.model_validate(record)


@router.get("/{user_id}", dependencies=[Depends(current_user)])
async def get_user(user_id: UserIdPath, session: Session) -> UserRead:
    """
    Get one user.
    \f
    :param user_id: The user's subject ID.
    :param session: The request's database session.
    :return: The user.
    """
    return UserRead.model_validate(await _get(session, user_id))


@router.patch("/{user_id}")
async def update_user(
    user_id: UserIdPath,
    body: UserUpdate,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> UserRead:
    """
    Change a user's names, email or MS ID. Their ID, and so their roles, stay.
    \f
    :param user_id: The user's subject ID.
    :param body: The fields to change.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The updated user.
    """
    await authorize(session, enforcer, user, "users:update", SITE)
    record = await _get(session, user_id)
    await _require_unique(
        session, email=body.email, msid=body.msid, other_than=record.id
    )
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(record, field, value)
    await commit_or_conflict(session, TAKEN)
    return UserRead.model_validate(record)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UserIdPath, user: CurrentUser, session: Session, enforcer: Enforcer
) -> None:
    """
    Remove a user, and revoke every role they hold, everywhere.
    \f
    :param user_id: The user's subject ID.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    await authorize(session, enforcer, user, "users:delete", SITE)
    record = await _get(session, user_id)
    # Revoking the caller's own roles would lock them out.
    if record.id == user:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't remove yourself")
    await session.execute(
        delete(CasbinRule).where(CasbinRule.ptype == "g", CasbinRule.v0 == record.id)
    )
    await session.delete(record)
    await session.commit()
