import json

import httpx2 as httpx
import pytest

from forge_codegraph import CodeGraph, WorkerError


def graph(handler) -> CodeGraph:
    return CodeGraph(
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://worker",
            headers={"Authorization": "Bearer token"},
        )
    )


async def test_read_sends_the_query_without_empty_values() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"nodes": []})

    answer = await graph(handler).read(
        "repo:abc", "neighbors", {"node": "entity:1", "cursor": "", "limit": 50}
    )
    assert answer == {"nodes": []}
    assert seen[0].url.path == "/v1/repositories/repo:abc/neighbors"
    assert dict(seen[0].url.params) == {"node": "entity:1", "limit": "50"}
    assert seen[0].headers["Authorization"] == "Bearer token"


async def test_read_refuses_what_the_worker_does_not_serve() -> None:
    with pytest.raises(ValueError):
        await graph(lambda _: httpx.Response(200, json={})).read(
            "repo:abc", "cross-links"
        )


async def test_search_posts_the_repositories_and_answers_the_hits() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"semantic": True, "hits": [{"id": "entity:1"}, "junk"]}
        )

    hits = await graph(handler).search(["repo:a", "repo:b"], "signing", limit=5)
    assert hits == [{"id": "entity:1"}]
    assert bodies == [
        {
            "repository_ids": ["repo:a", "repo:b"],
            "query": "signing",
            "limit": 5,
            "source": True,
        }
    ]


async def test_search_of_no_repositories_calls_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("called the worker")

    assert await graph(handler).search([], "signing") == []


async def test_errors_carry_the_workers_status_and_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"code": "not_found", "message": "node not found"}
        )

    with pytest.raises(WorkerError) as refused:
        await graph(handler).read("repo:abc", "node", {"node": "entity:9"})
    assert (refused.value.status, refused.value.code, refused.value.message) == (
        404,
        "not_found",
        "node not found",
    )


async def test_an_answer_that_is_not_json_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(WorkerError) as refused:
        await graph(handler).read("repo:abc", "stats")
    assert (refused.value.status, refused.value.code) == (502, "invalid_response")


async def test_an_unreachable_worker_is_status_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(WorkerError) as refused:
        await graph(handler).search(["repo:a"], "signing")
    assert (refused.value.status, refused.value.code) == (0, "unreachable")
