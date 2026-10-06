"""Organizations' API keys, apart from the routes: their form, their
subjects, which no person may take, and how using one is noted."""

import re
from datetime import timedelta
from typing import Any

import pytest
from key_world import ORG, World
from pydantic import SecretStr

from forge_admin.auth.api_keys import (
    KEY_PATTERN,
    ApiKeyError,
    key_digest,
    key_id_of,
    key_subject,
    looks_like_key,
    new_key,
    verify_key,
)
from forge_admin.auth.authorization import is_reserved_subject
from forge_admin.auth.tokens import TokenError, mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.audit import utc_now
from forge_admin.models import ApiKey


def test_a_new_key_is_random_and_kept_as_its_digest() -> None:
    one, two = new_key(), new_key()
    assert re.fullmatch(KEY_PATTERN, one.value)
    assert one.value != two.value
    assert one.sha256 == key_digest(one.value) and len(one.sha256) == 64
    assert one.hint == one.value[-4:]
    assert looks_like_key(one.value) and not looks_like_key("eyJhbGciOi.x.y")
    assert not looks_like_key(None)


def test_a_keys_subject_is_its_id_after_apikey() -> None:
    assert key_subject("abc") == "apikey:abc"
    assert key_id_of("apikey:abc") == "abc"
    assert key_id_of("user-1") is None and key_id_of(None) is None


def test_no_person_may_have_a_keys_subject(settings: Settings) -> None:
    assert is_reserved_subject("apikey:abc")
    signed = settings.model_copy(update={"jwt_secret": SecretStr("s" * 40)})
    with pytest.raises(TokenError):
        mint_subject_token(signed, "apikey:abc", lifetime=timedelta(minutes=1))


def test_using_a_key_is_noted_every_few_minutes(world: World) -> None:
    made = world.make_key(name="CI")

    async def use(at: Any) -> Any:
        return await verify_key(world.sessions, made["secret"], now=at)

    async def last_used(session: Any) -> Any:
        key = await session.get(ApiKey, made["id"])
        await session.refresh(key)
        return key.last_used_at

    start = utc_now()
    identity = world.client.portal.call(use, start)  # type: ignore[union-attr]
    assert identity.subject == made["subject"] and identity.organization_id == ORG
    assert world.run(last_used) == start
    world.client.portal.call(use, start + timedelta(minutes=1))  # type: ignore[union-attr]
    assert world.run(last_used) == start
    later = start + timedelta(minutes=6)
    world.client.portal.call(use, later)  # type: ignore[union-attr]
    assert world.run(last_used) == later


def test_a_key_that_isnt_one_is_refused(world: World) -> None:
    async def check(value: str) -> Any:
        return await verify_key(world.sessions, value)

    for value in ("fk_short", "fk_" + "x" * 43, "not-a-key"):
        with pytest.raises(ApiKeyError):
            world.client.portal.call(check, value)  # type: ignore[union-attr]
