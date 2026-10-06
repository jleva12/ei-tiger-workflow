"""
An organization's API keys: what outside apps send instead of a person's
sign-in, each holding one of the organization's roles as a member would
(``forge_admin.auth.api_keys``). The usual one is API caller (``org:api``),
which calls the organization's agents and workflows through the runtime.

Managing keys needs ``api_keys:manage`` in the organization (its
administrators have it), and a person: a key never manages keys. A key's
secret is answered once, when it's made; afterwards only its last four
characters. A key may only be given a role whose every permission its
maker holds there, so keys can't raise anyone's access.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

import casbin
from fastapi import APIRouter, HTTPException, Path, status
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.common import (
    Audited,
    Name,
    NodeId,
    Session,
    as_read,
    commit_or_conflict,
    name_of,
)
from forge_admin.auth.access import (
    CurrentPerson,
    Enforcer,
    Level,
    Scope,
    allows,
    assignment_pattern,
    authorize,
)
from forge_admin.auth.api_keys import hint_of, is_expired, key_subject, new_key
from forge_admin.auth.authorization import role_grants, roles_of_subjects
from forge_admin.db.audit import UtcDateTime, utc_now
from forge_admin.models import ApiKey, CasbinRule, Role
from forge_admin.models.api_keys import new_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["api keys"])

MANAGE = "api_keys:manage"
DEFAULT_ROLE = "org:api"
#: How far ahead a key may expire.
LONGEST = timedelta(days=366 * 2)

NAME_TAKEN = "The organization already has an API key by that name"
NOT_FOUND = "No such API key"

KeyId = Annotated[str, Path(pattern=r"^[0-9a-f-]{36}$")]


class ApiKeyCreate(BaseModel):
    name: Name
    #: One of the organization's roles (``org:…``).
    role: str = DEFAULT_ROLE
    #: When it stops working; None for never.
    expires_at: AwareDatetime | None = None


class ApiKeyUpdate(BaseModel):
    name: Name | None = None
    role: str | None = None


class ApiKeyRead(Audited):
    id: str
    organization_id: str
    name: str
    #: Its role there; None once the role has been deleted.
    role: str | None
    role_name: str | None
    #: How it's shown: ``fk_…`` and its last four characters.
    hint: str
    #: Its Casbin subject, as runs and usage name its calls.
    subject: str
    expires_at: UtcDateTime | None
    expired: bool
    last_used_at: UtcDateTime | None
    created_by_name: str


class ApiKeyCreated(ApiKeyRead):
    #: The key itself: answered this once, never again.
    secret: str


async def _read(
    session: AsyncSession, key: ApiKey, roles: dict[str, str] | None = None
) -> ApiKeyRead:
    subject = key_subject(key.id)
    if roles is None:
        roles = await roles_of_subjects(
            session, [subject], assignment_pattern(f"org:{key.organization_id}")
        )
    role = roles.get(subject)
    record = await session.get(Role, role) if role else None
    return as_read(
        ApiKeyRead,
        key,
        role=role,
        role_name=record.name if record else None,
        hint=hint_of(key),
        subject=subject,
        expired=is_expired(key),
        created_by_name=await name_of(session, key.created_by),
    )


async def _key_of(session: AsyncSession, organization_id: str, key_id: str) -> ApiKey:
    key = await session.get(ApiKey, key_id)
    if key is None or key.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return key


async def _grantable_role(
    session: AsyncSession,
    enforcer: casbin.AsyncEnforcer,
    user: str,
    role_key: str,
    domain: str,
) -> Role:
    """
    The role a key may be given: one of an organization's, whose every
    grant the person giving it holds there (the policy is loaded: authorize
    ran first).

    :raises HTTPException: 422 for an unknown or site role; 403 for one that
        grants more than they hold.
    """
    role = await session.get(Role, role_key)
    if role is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown role {role_key}"
        )
    if role.level != Level.ORG:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{role_key} isn't an organization's role",
        )
    lacking = [
        f"{resource}:{action}"
        for resource, action in await role_grants(session, role_key)
        if not allows(enforcer, user, f"{resource}:{action}", domain)
    ]
    if lacking:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can't give a key permissions you don't hold here: "
            + ", ".join(lacking),
        )
    return role


def _expiry(value: datetime | None) -> datetime | None:
    """:return: When a new key expires, as MySQL keeps times (naive UTC)."""
    if value is None:
        return None
    when = value.astimezone(UTC).replace(tzinfo=None)
    now = utc_now()
    if when <= now:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "It must expire in the future"
        )
    if when - now > LONGEST:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "It must expire within two years, or never",
        )
    return when


@router.get("/organizations/{organization_id}/api-keys")
async def list_api_keys(
    organization_id: NodeId, user: CurrentPerson, session: Session, enforcer: Enforcer
) -> list[ApiKeyRead]:
    """
    List the organization's API keys, newest first, with their roles: never
    their secrets.
    \f
    :raises HTTPException: 403 without api_keys:manage in the organization.
    """
    domain = await authorize(
        session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id)
    )
    keys = list(
        await session.scalars(
            select(ApiKey)
            .where(ApiKey.organization_id == organization_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.name)
        )
    )
    roles = await roles_of_subjects(
        session, [key_subject(key.id) for key in keys], assignment_pattern(domain)
    )
    return [await _read(session, key, roles) for key in keys]


@router.post(
    "/organizations/{organization_id}/api-keys", status_code=status.HTTP_201_CREATED
)
async def create_api_key(
    organization_id: NodeId,
    body: ApiKeyCreate,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> ApiKeyCreated:
    """
    Make an API key holding one of the organization's roles. The answer
    carries the key itself, this once: copy it now.
    \f
    :raises HTTPException: 403 without api_keys:manage in the organization,
        or for a role granting more than the caller holds there; 409 when
        the name is taken; 422 for an unknown or site role, or an expiry in
        the past or more than two years ahead.
    """
    domain = await authorize(
        session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id)
    )
    role = await _grantable_role(session, enforcer, user, body.role, domain)
    made = new_key()
    key = ApiKey(
        # Its ID now, for its role's line: both are written together.
        id=new_id(),
        organization_id=organization_id,
        name=body.name,
        secret_sha256=made.sha256,
        hint=made.hint,
        expires_at=_expiry(body.expires_at),
    )
    session.add(key)
    session.add(
        CasbinRule(
            ptype="g",
            v0=key_subject(key.id),
            v1=role.key,
            v2=assignment_pattern(domain),
        )
    )
    await commit_or_conflict(session, NAME_TAKEN)
    logger.info(
        "%s created API key %s (%s) in organization %s with role %s",
        user,
        key.name,
        key.id,
        organization_id,
        role.key,
    )
    read = await _read(session, key, {key_subject(key.id): role.key})
    return ApiKeyCreated(**read.model_dump(), secret=made.value)


@router.get("/organizations/{organization_id}/api-keys/{key_id}")
async def get_api_key(
    organization_id: NodeId,
    key_id: KeyId,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> ApiKeyRead:
    """
    Read one API key: never its secret.
    \f
    :raises HTTPException: 403 without api_keys:manage in the organization;
        404 for another organization's key, or none.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    return await _read(session, await _key_of(session, organization_id, key_id))


@router.patch("/organizations/{organization_id}/api-keys/{key_id}")
async def update_api_key(
    organization_id: NodeId,
    key_id: KeyId,
    body: ApiKeyUpdate,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> ApiKeyRead:
    """
    Rename an API key, or give it another role. Apps using it keep using it;
    a new role applies from their next call.
    \f
    :raises HTTPException: 403 without api_keys:manage in the organization,
        or for a role granting more than the caller holds there; 404 for
        another organization's key, or none; 409 when the name is taken;
        422 for an unknown or site role.
    """
    domain = await authorize(
        session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id)
    )
    key = await _key_of(session, organization_id, key_id)
    if body.name is not None:
        key.name = body.name
    if body.role is not None:
        role = await _grantable_role(session, enforcer, user, body.role, domain)
        subject = key_subject(key.id)
        await session.execute(
            delete(CasbinRule).where(CasbinRule.ptype == "g", CasbinRule.v0 == subject)
        )
        session.add(
            CasbinRule(
                ptype="g", v0=subject, v1=role.key, v2=assignment_pattern(domain)
            )
        )
        logger.info("%s gave API key %s the role %s", user, key.id, role.key)
    await commit_or_conflict(session, NAME_TAKEN)
    return await _read(session, key)


@router.delete(
    "/organizations/{organization_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_api_key(
    organization_id: NodeId,
    key_id: KeyId,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Delete an API key, and its role with it: apps using it are refused from
    their next call.
    \f
    :raises HTTPException: 403 without api_keys:manage in the organization;
        404 for another organization's key, or none.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    key = await _key_of(session, organization_id, key_id)
    await session.execute(
        delete(CasbinRule).where(
            CasbinRule.ptype == "g", CasbinRule.v0 == key_subject(key.id)
        )
    )
    await session.delete(key)
    await session.commit()
    logger.info(
        "%s deleted API key %s (%s) in organization %s",
        user,
        key.name,
        key.id,
        organization_id,
    )
