"""Bearer tokens: JWTs whose ``sub`` is a user's ID.

The API verifies them on every request (``auth.security.authenticate``),
signed HS256 with ``FORGE_ADMIN_JWT_SECRET`` and, when configured, issued by
``FORGE_ADMIN_JWT_ISSUER`` for ``FORGE_ADMIN_JWT_AUDIENCE``. For local
development, ``forge-admin-token`` (``cli/token.py``) mints one for a user,
e.g. for the web console's ``VITE_API_TOKEN`` (``make web-token``). The
assistant mints short-lived ones for the person it is talking to
(``mint_subject_token``), to call other Forge services as them.
"""

import re
from datetime import datetime, timedelta
from typing import Any

import jwt

from forge_admin.auth.authorization import ROLE_KEY_PATTERN, SUBJECT_PATTERN
from forge_admin.config import Settings
from forge_admin.db.audit import utc_now
from forge_admin.models import User

ALGORITHM = "HS256"


class TokenError(Exception):
    """A bearer token the API won't accept; the message says why."""


def mint_token(
    settings: Settings,
    user: User,
    *,
    lifetime: timedelta,
    now: datetime | None = None,
) -> str:
    """
    Sign a token that identifies a user.

    Besides ``sub``, it carries the user's profile as an identity provider's
    would (``email``, ``given_name``, ``family_name``, ``name``, ``msid``); the
    API reads only ``sub``.

    :param settings: Settings with ``jwt_secret``.
    :param user: The user it identifies.
    :param lifetime: How long it is valid.
    :param now: When it is issued; defaults to the current time.
    :return: The encoded JWT.
    :raises TokenError: No ``jwt_secret`` is configured.
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
    return _sign(settings, claims)


def mint_subject_token(
    settings: Settings,
    subject: str,
    *,
    lifetime: timedelta,
    now: datetime | None = None,
) -> str:
    """
    Sign a token that only identifies a user, for this API's own calls on
    their behalf: the assistant calls this API's routes as the person it is
    talking to.

    :param settings: Settings with ``jwt_secret``.
    :param subject: The user's ID.
    :param lifetime: How long it is valid; keep it short.
    :param now: When it is issued; defaults to the current time.
    :return: The encoded JWT.
    :raises TokenError: No ``jwt_secret`` is configured, or the subject isn't
        one ``verify_token`` would accept.
    """
    if not re.fullmatch(SUBJECT_PATTERN, subject) or re.fullmatch(
        ROLE_KEY_PATTERN, subject
    ):
        raise TokenError("Invalid token subject")
    issued = now or utc_now()
    return _sign(settings, {"sub": subject, "iat": issued, "exp": issued + lifetime})


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

    :param settings: The API's settings.
    :param token: The encoded JWT.
    :return: Its ``sub``: the user's ID.
    :raises TokenError: Tokens aren't accepted, or this one is expired,
        wrongly signed, for another issuer or audience, or lacks a valid
        ``sub``, ``iat`` or ``exp``.
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
    # Roles and subjects share Casbin's namespace; see members.assign_role.
    if not re.fullmatch(SUBJECT_PATTERN, subject) or re.fullmatch(
        ROLE_KEY_PATTERN, subject
    ):
        raise TokenError("Invalid bearer token subject")
    return str(subject)
