"""Bearer tokens: JWTs whose ``sub`` is a user's ID.

The API verifies them on every request (``auth.security.authenticate``),
signed HS256 with ``FORGE_ADMIN_JWT_SECRET`` and, when configured, issued by
``FORGE_ADMIN_JWT_ISSUER`` for ``FORGE_ADMIN_JWT_AUDIENCE``. For local
development, ``forge-admin-token`` (``cli/token.py``) mints one for a user,
e.g. for the web console's ``VITE_API_TOKEN`` (``make web-token``). The
assistant mints short-lived ones for the person it is talking to
(``mint_subject_token``), to call other Forge services as them.

A token may also name the organization it acts in (``ORGANIZATION_CLAIM``):
``forge-admin-token --organization`` mints one, for services that serve one
organization's data at a time, such as the code graph's MCP server, which
reads only that organization's repositories (``GET /code-graph/access``).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import jwt

from forge_admin.auth.access import UUID_PATTERN
from forge_admin.auth.authorization import (
    GROUP_NAME_PATTERN,
    SUBJECT_PATTERN,
    is_reserved_subject,
)
from forge_admin.config import Settings
from forge_admin.db.audit import utc_now
from forge_admin.models import User

ALGORITHM = "HS256"
#: The claim naming the organization a token acts in, by ID.
ORGANIZATION_CLAIM = "org_id"


# The most groups read from a token; a directory's long tail stops here.
MAX_GROUPS = 500


class TokenError(Exception):
    """A bearer token the API won't accept; the message says why."""


@dataclass(frozen=True)
class Identity:
    """
    Who a token identifies: the user, the company groups they're in, and the
    organization it was minted for, if it names one.
    """

    subject: str
    groups: tuple[str, ...] = ()
    organization_id: str | None = None


def mint_token(
    settings: Settings,
    user: User,
    *,
    lifetime: timedelta,
    now: datetime | None = None,
    groups: list[str] | None = None,
    organization_id: str | None = None,
) -> str:
    """
    Sign a token that identifies a user.

    Besides ``sub``, it carries the user's profile as an identity provider's
    would (``email``, ``given_name``, ``family_name``, ``name``, ``msid``); the
    API reads only ``sub``, the groups claim when ``groups`` are given, and
    ``ORGANIZATION_CLAIM`` when it's minted for an organization.

    :param settings: Settings with ``jwt_secret``.
    :param user: The user it identifies.
    :param lifetime: How long it is valid.
    :param now: When it is issued; defaults to the current time.
    :param groups: The company groups it says they're in.
    :param organization_id: The organization it acts in.
    :return: The encoded JWT.
    :raises TokenError: No ``jwt_secret`` is configured, or the organization
        ID isn't one.
    """
    issued = now or utc_now()
    claims: dict[str, Any] = {
        "sub": user.id,
        "iat": issued,
        "exp": issued + lifetime,
        "email": user.email,
        "given_name": user.first_name,
        "family_name": user.last_name,
        "name": f"{user.first_name} {user.last_name}",
        "msid": user.msid,
    }
    if groups:
        claims[settings.jwt_groups_claim] = list(groups)
    return _sign(settings, _for_organization(claims, organization_id))


def mint_subject_token(
    settings: Settings,
    subject: str,
    *,
    lifetime: timedelta,
    now: datetime | None = None,
    organization_id: str | None = None,
) -> str:
    """
    Sign a token that only identifies a user, for this API's own calls on
    their behalf: the assistant calls this API's routes as the person it is
    talking to.

    :param settings: Settings with ``jwt_secret``.
    :param subject: The user's ID.
    :param lifetime: How long it is valid; keep it short.
    :param now: When it is issued; defaults to the current time.
    :param organization_id: The organization it acts in, for a service that
        serves one organization's data at a time.
    :return: The encoded JWT.
    :raises TokenError: No ``jwt_secret`` is configured, or the subject or
        organization ID isn't one ``verify_identity`` would accept.
    """
    if not re.fullmatch(SUBJECT_PATTERN, subject) or is_reserved_subject(subject):
        raise TokenError("Invalid token subject")
    issued = now or utc_now()
    claims: dict[str, Any] = {"sub": subject, "iat": issued, "exp": issued + lifetime}
    return _sign(settings, _for_organization(claims, organization_id))


def _for_organization(
    claims: dict[str, Any], organization_id: str | None
) -> dict[str, Any]:
    if organization_id is not None:
        if not re.fullmatch(UUID_PATTERN, organization_id):
            raise TokenError("Invalid token organization")
        claims[ORGANIZATION_CLAIM] = organization_id
    return claims


def _sign(settings: Settings, claims: dict[str, Any]) -> str:
    if settings.jwt_secret is None:
        raise TokenError("FORGE_ADMIN_JWT_SECRET is not set")
    if settings.jwt_issuer:
        claims["iss"] = settings.jwt_issuer
    if settings.jwt_audience:
        claims["aud"] = settings.jwt_audience
    return jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM
    )


def verify_token(settings: Settings, token: str) -> str:
    """
    Check a bearer token and return the user it identifies.

    :raises TokenError: As :func:`verify_identity`.
    """
    return verify_identity(settings, token).subject


def groups_of(value: object) -> tuple[str, ...]:
    """
    The company groups a token's groups claim names: a list of names (or
    one name). Names that couldn't be a group's are left out.
    """
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list):
        return ()
    found = dict.fromkeys(
        name
        for name in names[:MAX_GROUPS]
        if isinstance(name, str) and re.fullmatch(GROUP_NAME_PATTERN, name)
    )
    return tuple(found)


def verify_identity(settings: Settings, token: str) -> Identity:
    """
    Check a bearer token and return who it identifies: the user, the
    company groups its ``jwt_groups_claim`` names, and the organization its
    ``ORGANIZATION_CLAIM`` names.

    :param settings: The API's settings.
    :param token: The encoded JWT.
    :return: Its ``sub`` (the user's ID), its groups and organization.
    :raises TokenError: Tokens aren't accepted, or this one is expired,
        wrongly signed, for another issuer or audience, lacks a valid
        ``sub``, ``iat`` or ``exp``, or names an organization by something
        that isn't an ID.
    """
    if settings.jwt_secret is None:
        raise TokenError("This API does not accept bearer tokens")
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise TokenError("The bearer token has expired") from None
    except jwt.InvalidTokenError:
        raise TokenError("Invalid bearer token") from None
    subject = claims["sub"]
    # Roles, groups and subjects share Casbin's namespace; see members.assign_role.
    if not re.fullmatch(SUBJECT_PATTERN, subject) or is_reserved_subject(subject):
        raise TokenError("Invalid bearer token subject")
    organization = claims.get(ORGANIZATION_CLAIM)
    if organization is not None and not (
        isinstance(organization, str) and re.fullmatch(UUID_PATTERN, organization)
    ):
        raise TokenError("Invalid bearer token organization")
    return Identity(
        str(subject), groups_of(claims.get(settings.jwt_groups_claim)), organization
    )
