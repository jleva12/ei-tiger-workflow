"""Where a request came from and how its caller signed in
(``forge_admin.auth.origin``), as the security dependency records it for what
the request starts; and the assistant's tokens, which say it acts for the
person."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from forge_task_adk_workflows.run_store import Actor
from pydantic import SecretStr
from starlette.datastructures import Headers

from forge_admin.adk_workflows.provenance import console_start
from forge_admin.api.server import ApiServer
from forge_admin.assistant.user_tokens import UserTokens
from forge_admin.auth.origin import (
    RequestOrigin,
    current_origin,
    origin_of,
    set_origin,
)
from forge_admin.auth.tokens import (
    ACTOR_CLAIM,
    TokenError,
    mint_subject_token,
    verify_identity,
)
from forge_admin.config import Settings

SECRET = "test-secret-that-is-at-least-32-chars"
ADA = "d2a66448-9446-46be-81ca-a8c73197df05"

router = APIRouter()


@router.get("/origin")
async def origin(request: Request) -> dict[str, str | None]:
    found = request.state.origin
    assert found == current_origin()
    return {
        "client_ip": found.client_ip,
        "user_agent": found.user_agent,
        "request_id": found.request_id,
        "auth_method": found.auth_method,
        "actor": found.actor,
    }


@pytest.fixture
def configured(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"jwt_secret": SecretStr(SECRET), "local_user_id": None}
    )


def test_a_request_records_where_it_came_from_and_how_its_caller_signed_in(
    configured: Settings,
) -> None:
    app = ApiServer(configured, routers=[router]).create_app()
    token = mint_subject_token(configured, ADA, lifetime=timedelta(minutes=5))
    with TestClient(app) as client:
        signed_in = client.get(
            "/api/v1/origin",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Forge console",
                "X-Request-ID": "req-1",
            },
        ).json()
        nobody = client.get("/api/v1/origin").json()
    assert signed_in == {
        "client_ip": "testclient",
        "user_agent": "Forge console",
        "request_id": "req-1",
        "auth_method": "jwt",
        "actor": None,
    }
    assert nobody["auth_method"] == "anonymous"
    # Its own ID, when it sent none.
    assert nobody["request_id"] and nobody["request_id"] != "req-1"


def test_the_local_development_user_signed_in_with_nothing(
    configured: Settings,
) -> None:
    local = configured.model_copy(update={"local_user_id": ADA})
    app = ApiServer(local, routers=[router]).create_app()
    with TestClient(app) as client:
        assert client.get("/api/v1/origin").json()["auth_method"] == "local"


def request_from(ip: str, agent: str, request_id: str) -> Request:
    scope = {
        "type": "http",
        "client": (ip, 1234),
        "headers": Headers({"user-agent": agent}).raw,
        "state": {"request_id": request_id},
    }
    return Request(scope)


def test_the_assistants_call_keeps_the_persons_request_it_serves() -> None:
    async def check() -> None:
        person = origin_of(
            request_from("198.51.100.4", "Browser", "req-person"), auth_method="jwt"
        )
        set_origin(person)
        # Its in-process call, as the person, saying the assistant acts for them.
        inner = origin_of(
            request_from("127.0.0.1", "python-httpx", "req-inner"),
            auth_method="jwt",
            actor="assistant",
        )
        assert inner == RequestOrigin(
            client_ip="198.51.100.4",
            user_agent="Browser",
            request_id="req-person",
            auth_method="jwt",
            actor="assistant",
        )
        # A call nobody acts in is its own.
        own = origin_of(request_from("127.0.0.1", "curl", "req-2"), auth_method="jwt")
        assert own.client_ip == "127.0.0.1"

    asyncio.run(check())


def test_an_assistant_token_says_the_assistant_acts_for_the_person(
    configured: Settings,
) -> None:
    token = UserTokens(configured).token(ADA)
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims[ACTOR_CLAIM] == {"sub": "assistant"}
    identity = verify_identity(configured, token)
    assert (identity.subject, identity.actor) == (ADA, "assistant")
    # A token nobody acts in, or whose actor isn't one, has none.
    plain = mint_subject_token(configured, ADA, lifetime=timedelta(minutes=5))
    assert verify_identity(configured, plain).actor is None
    odd = jwt.encode(
        {**claims, ACTOR_CLAIM: {"sub": "Robert'); DROP"}}, SECRET, algorithm="HS256"
    )
    assert verify_identity(configured, odd).actor is None
    with pytest.raises(TokenError, match="actor"):
        mint_subject_token(
            configured, ADA, lifetime=timedelta(minutes=5), actor="Not An Actor"
        )


@pytest.mark.parametrize(
    ("starter", "origin", "client", "expected"),
    [
        (
            "u-1",
            RequestOrigin(auth_method="jwt"),
            "builder",
            ("builder_test", "console"),
        ),
        ("u-1", RequestOrigin(auth_method="jwt"), "console", ("manual", "console")),
        ("u-1", RequestOrigin(auth_method="jwt"), None, ("manual", "api")),
        (
            "u-1",
            RequestOrigin(auth_method="jwt", actor="assistant"),
            "builder",
            ("assistant", "assistant"),
        ),
        (
            "apikey:k-1",
            RequestOrigin(auth_method="api_key"),
            "builder",
            ("api_key", "api"),
        ),
    ],
)
def test_a_console_start_is_told_apart_by_who_and_what_called(
    starter: str,
    origin: RequestOrigin,
    client: str | None,
    expected: tuple[str, str],
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(origin=origin),
        headers={"X-Forge-Client": client} if client else {},
    )
    started = console_start(request, Actor(starter, "Someone"))  # type: ignore[arg-type]
    assert (started.trigger_type, started.channel) == expected
    assert started.on_behalf_of == starter and started.on_behalf_of_verified is True
    assert started.api_key_id == ("k-1" if starter.startswith("apikey:") else None)


def test_the_local_development_users_start_isnt_verified() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(origin=RequestOrigin(auth_method="local")), headers={}
    )
    started = console_start(request, Actor("u-1", "Dev"))  # type: ignore[arg-type]
    assert (started.auth_method, started.on_behalf_of_verified) == ("local", False)
