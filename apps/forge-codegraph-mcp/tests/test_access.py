"""Forge credentials, checked with the admin API: what's kept, for how long,
and the scope a caller reads."""

import asyncio
from typing import Any

import httpx
import pytest
from conftest import REPO, SHOP_URL, make_settings

from forge_codegraph_mcp.access import (
    READ,
    AdminUnavailable,
    ForgeAccessVerifier,
    RepositoryScope,
    current_scope,
    repository_id,
)
from forge_codegraph_mcp.graph.errors import NotFound

ORG = "3f6c0000-0000-4000-8000-000000000001"


def granted(*urls: str) -> dict[str, Any]:
    return {
        "subject": "apikey:k1",
        "organization_id": ORG,
        "repositories": [{"url": url, "owner": "o", "name": "n", "branch": "main"} for url in urls],
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def verifier(handle: Any, clock: Clock | None = None, **auth: Any) -> ForgeAccessVerifier:
    settings = make_settings(mcp={"auth": auth})
    return ForgeAccessVerifier(
        settings.mcp.auth, transport=httpx.MockTransport(handle), clock=clock or Clock()
    )


async def test_a_granted_credential_reads_its_organizations_repositories() -> None:
    check = verifier(lambda _: httpx.Response(200, json=granted(SHOP_URL)))
    access = await check.verify_token("fk_x")
    assert access is not None and access.scopes == [READ]
    scope = RepositoryScope.of(access)
    assert scope == RepositoryScope("apikey:k1", ORG, frozenset({REPO}))
    assert scope.readable(REPO) and not scope.readable(repository_id("https://github.com/x/y"))
    scope.check(REPO)
    with pytest.raises(NotFound, match="repository repo:other"):
        scope.check(REPO, "repo:other")


async def test_answers_are_kept_until_they_lapse() -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers["Authorization"])
        return httpx.Response(200, json=granted(SHOP_URL))

    clock = Clock()
    check = verifier(handle, clock, cache_seconds=30)
    await check.verify_token("fk_a")
    clock.now += 29
    await check.verify_token("fk_a")
    assert len(asked) == 1
    clock.now += 2
    await check.verify_token("fk_a")
    assert len(asked) == 2
    await check.verify_token("fk_b")
    assert asked == ["Bearer fk_a", "Bearer fk_a", "Bearer fk_b"]


async def test_at_most_cache_size_answers_are_kept() -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers["Authorization"])
        return httpx.Response(200, json=granted())

    check = verifier(handle, cache_size=2)
    for credential in ("fk_a", "fk_b", "fk_c", "fk_c", "fk_b", "fk_a"):
        await check.verify_token(credential)
    # fk_c pushed out fk_a, the oldest; fk_a's return pushed out fk_b.
    assert asked == ["Bearer fk_a", "Bearer fk_b", "Bearer fk_c", "Bearer fk_a"]


async def test_calls_together_share_one_question() -> None:
    asked = 0
    gate = asyncio.Event()

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal asked
        asked += 1
        await gate.wait()
        return httpx.Response(200, json=granted(SHOP_URL))

    check = verifier(handle)
    waiting = [asyncio.create_task(check.verify_token("fk_a")) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    answers = await asyncio.gather(*waiting)
    assert asked == 1 and all(a is not None and a.scopes == [READ] for a in answers)


async def test_refusals_and_failures() -> None:
    answers = {
        "Bearer fk_unknown": httpx.Response(401, json={"detail": "Unknown API key"}),
        "Bearer fk_forbidden": httpx.Response(403, json={"detail": "Requires repositories:read"}),
        "Bearer fk_gone": httpx.Response(404, json={"detail": "Organization not found"}),
        "Bearer fk_broken": httpx.Response(502),
        "Bearer fk_odd": httpx.Response(200, json={"repositories": []}),
    }
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers["Authorization"])
        return answers[request.headers["Authorization"]]

    check = verifier(handle)
    assert await check.verify_token("fk_unknown") is None
    for credential in ("fk_forbidden", "fk_gone"):
        # Known, but reading nothing: the endpoint answers 403.
        access = await check.verify_token(credential)
        assert access is not None and access.scopes == []
    for credential in ("fk_broken", "fk_odd"):
        with pytest.raises(AdminUnavailable):
            await check.verify_token(credential)
    # Refusals and failures are asked again; known credentials aren't.
    for credential in ("fk_unknown", "fk_forbidden", "fk_gone", "fk_broken"):
        await asyncio.gather(check.verify_token(credential), return_exceptions=True)
    assert asked.count("Bearer fk_unknown") == 2
    assert asked.count("Bearer fk_forbidden") == asked.count("Bearer fk_gone") == 1
    assert asked.count("Bearer fk_broken") == 2


async def test_an_unreachable_admin_api_is_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AdminUnavailable, match="can't be reached"):
        await verifier(handle).verify_token("fk_a")


def test_outside_a_request_there_is_no_scope() -> None:
    with pytest.raises(PermissionError):
        current_scope()


async def test_the_scope_carries_the_organizations_system_maps() -> None:
    other = "https://github.com/acme/billing"
    end = {
        "node_id": "entity:1",
        "kind": "method",
        "name": "charge",
        "qualified_name": "shop.charge",
        "path": "shop.py",
    }
    answer = granted(SHOP_URL, other) | {
        "link_owners": ["kb:1"],
        "connections": [
            {
                "id": "c1",
                "knowledge_base_id": "1",
                "knowledge_base": "Checkout",
                "kind": "calls",
                "description": "POST /charges",
                "source": {"url": SHOP_URL, "owner": "acme", "name": "shop"},
                "target": {"url": other, "owner": "acme", "name": "billing"},
                "code_links": [{"source": end, "target": end, "label": "POST /charges"}],
            }
        ],
    }
    access = await verifier(lambda _: httpx.Response(200, json=answer)).verify_token("fk_x")
    assert access is not None
    scope = RepositoryScope.of(access)
    assert scope.link_owners == frozenset({"kb:1"})
    assert scope.owns("kb:1") and not scope.owns("kb:2")
    [connection] = scope.connections
    assert connection["source"] == {"repository_id": REPO, "name": "acme/shop"}
    assert connection["target"]["repository_id"] == repository_id(other)
    assert connection["code_links"][0]["label"] == "POST /charges"
    # An admin API from before system maps names no owners: every link is followed.
    old = await verifier(lambda _: httpx.Response(200, json=granted(SHOP_URL))).verify_token("fk_y")
    assert old is not None
    scope = RepositoryScope.of(old)
    assert scope.link_owners is None and scope.owns("kb:2") and scope.connections == ()
