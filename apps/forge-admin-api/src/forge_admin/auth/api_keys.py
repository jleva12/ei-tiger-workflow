"""Organizations' API keys: making them, and checking the ones requests send.

A key is ``fk_`` and 43 URL-safe characters (256 random bits), sent as
``Authorization: Bearer fk_…`` or ``X-API-Key: fk_…``. Only its SHA-256 is
kept, so a leaked table leaks no keys, and requests are looked up by it; a
salt would add nothing to a secret this random. Its Casbin subject is
``apikey:<id>``: what it may do is the role that subject holds in its
organization (forge_admin.models.ApiKey).
"""

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.auth.authorization import API_KEY_SUBJECT_PREFIX
from forge_admin.db.audit import utc_now
from forge_admin.models import ApiKey

logger = logging.getLogger(__name__)

KEY_PREFIX = "fk_"
KEY_PATTERN = rf"^{KEY_PREFIX}[A-Za-z0-9_-]{{43}}$"
#: The Casbin subject of a key is this and its ID.
SUBJECT_PREFIX = API_KEY_SUBJECT_PREFIX
#: How stale ``last_used_at`` may get before a request refreshes it: often
#: enough to tell keys in use from forgotten ones, without a write per call.
LAST_USED_EVERY = timedelta(minutes=5)


class ApiKeyError(Exception):
    """The key isn't one: malformed, unknown, or expired."""


@dataclass(frozen=True)
class NewKey:
    """A key just made: the value (shown once), its SHA-256 and hint."""

    value: str
    sha256: str
    hint: str


@dataclass(frozen=True)
class KeyIdentity:
    """Who a verified key is."""

    id: str
    name: str
    organization_id: str

    @property
    def subject(self) -> str:
        """:return: Its Casbin subject, ``apikey:<id>``."""
        return key_subject(self.id)


def new_key() -> NewKey:
    """:return: A new random key."""
    value = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewKey(value, key_digest(value), value[-4:])


def key_digest(value: str) -> str:
    """:return: The key's SHA-256, hex: what's stored and looked up."""
    return hashlib.sha256(value.encode()).hexdigest()


def looks_like_key(value: str | None) -> bool:
    """:return: Whether a credential is meant as an API key (its prefix)."""
    return bool(value) and value.startswith(KEY_PREFIX)  # type: ignore[union-attr]


def key_subject(key_id: str) -> str:
    """:return: A key's Casbin subject."""
    return SUBJECT_PREFIX + key_id


def key_id_of(subject: str | None) -> str | None:
    """:return: The key's ID when the subject is a key's, else None."""
    if subject and subject.startswith(SUBJECT_PREFIX):
        return subject[len(SUBJECT_PREFIX) :]
    return None


def hint_of(key: ApiKey) -> str:
    """:return: How the key is shown: its prefix and last four characters."""
    return f"{KEY_PREFIX}…{key.hint}"


def is_expired(key: ApiKey, now: datetime | None = None) -> bool:
    """:return: Whether the key has stopped working."""
    return key.expires_at is not None and key.expires_at <= (now or utc_now())


async def verify_key(
    sessions: async_sessionmaker[AsyncSession],
    value: str,
    *,
    now: datetime | None = None,
) -> KeyIdentity:
    """
    Check a key a request sent, in a session of its own.

    It's looked up by its SHA-256, so the comparison never sees the secret.
    When it was last used more than ``LAST_USED_EVERY`` ago, that's
    refreshed, leaving the row's audit columns alone (using a key isn't
    changing it); a failed refresh doesn't fail the request.

    :param sessions: Makes database sessions.
    :param value: The key, as sent.
    :param now: The time, for tests.
    :return: Whose it is.
    :raises ApiKeyError: Malformed, unknown, or expired.
    """
    if not re.fullmatch(KEY_PATTERN, value):
        raise ApiKeyError("That isn't an API key")
    now = now or utc_now()
    async with sessions() as session:
        key = await session.scalar(
            select(ApiKey).where(ApiKey.secret_sha256 == key_digest(value))
        )
        if key is None:
            raise ApiKeyError("Unknown API key")
        if is_expired(key, now):
            raise ApiKeyError("This API key has expired")
        identity = KeyIdentity(key.id, key.name, key.organization_id)
        if key.last_used_at is None or now - key.last_used_at >= LAST_USED_EVERY:
            try:
                await session.execute(
                    update(ApiKey)
                    .where(ApiKey.id == key.id)
                    .values(
                        last_used_at=now,
                        updated_at=ApiKey.updated_at,
                        updated_by=ApiKey.updated_by,
                    )
                )
                await session.commit()
            except Exception:  # noqa: BLE001 - noting the use is best effort
                logger.warning("Couldn't note API key %s's use", key.id, exc_info=True)
    return identity
