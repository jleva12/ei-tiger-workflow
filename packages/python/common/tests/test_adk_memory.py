import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import httpx
import pytest
from google.adk.events.event import Event
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types
from pymongo import AsyncMongoClient

from forge_common.adk.memory import AtlasVectorMemoryService, InputType, OpenAIEmbedder
from forge_common.model_provider import ModelProviderConfigError, parse_model_provider_yaml

# --- OpenAIEmbedder ------------------------------------------------------------


def _embedder(handler: Callable[[httpx.Request], httpx.Response], **options: Any) -> OpenAIEmbedder:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    options.setdefault("api_key", "sk-test")
    return OpenAIEmbedder(client=client, **options)


def _answer(requests: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append({"url": str(request.url), "headers": request.headers, "body": body})
        # Answered out of order: the embedder sorts by index.
        data = [
            {"index": i, "embedding": [float(len(text))]} for i, text in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": list(reversed(data))})

    return handler


async def test_embedder_sends_texts_in_batches_and_keeps_their_order() -> None:
    requests: list[dict[str, Any]] = []
    embedder = _embedder(
        _answer(requests),
        model="text-embedding-3-large",
        dimensions=256,
        base_url="https://gateway.example/v1/",
        headers={"x-tenant": "forge"},
        batch_size=2,
    )

    vectors = await embedder.embed(["a", "bb", "ccc"], input_type="query")

    assert vectors == [[1.0], [2.0], [3.0]]
    assert [request["body"]["input"] for request in requests] == [["a", "bb"], ["ccc"]]
    first = requests[0]
    assert first["url"] == "https://gateway.example/v1/embeddings"
    assert first["headers"]["authorization"] == "Bearer sk-test"
    assert first["headers"]["x-tenant"] == "forge"
    assert first["body"] == {
        "input": ["a", "bb"],
        "model": "text-embedding-3-large",
        "dimensions": 256,
        "encoding_format": "float",
    }


async def test_embedder_cuts_long_texts_and_keeps_requests_small() -> None:
    requests: list[dict[str, Any]] = []
    embedder = _embedder(_answer(requests), max_chars=5, batch_chars=8)

    vectors = await embedder.embed(["abcdefgh", "abc", "ab"], input_type="document")

    assert vectors == [[5.0], [3.0], [2.0]]
    assert [request["body"]["input"] for request in requests] == [["abcde", "abc"], ["ab"]]


async def test_embedder_asks_for_a_token_for_each_request() -> None:
    requests: list[dict[str, Any]] = []
    tokens = iter(["first", "second"])

    async def token() -> str:
        return next(tokens)

    embedder = _embedder(_answer(requests), api_key=None, token=token, batch_size=1)
    await embedder.embed(["a", "b"], input_type="document")

    assert [r["headers"]["authorization"] for r in requests] == ["Bearer first", "Bearer second"]


async def test_embedder_raises_with_what_the_api_said() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})

    with pytest.raises(httpx.HTTPStatusError, match="429.*Rate limit reached"):
        await _embedder(handler).embed(["a"], input_type="document")


def test_embedder_needs_a_key_or_a_token() -> None:
    async def token() -> str:
        return "t"

    with pytest.raises(ValueError):
        OpenAIEmbedder()
    with pytest.raises(ValueError):
        OpenAIEmbedder("sk-test", token=token)


PROVIDERS = """
version: 1
default: {provider: openai, model: gpt-5}
providers:
  openai:
    baseUrl: https://gateway.example/openai/v1
    api: openai-responses
    headers: {x-tenant: forge}
    auth: {type: apiKey, key: sk-provider}
    models: [{id: gpt-5}]
  google:
    baseUrl: https://generativelanguage.googleapis.com/v1beta
    api: google-generative-ai
    auth: {type: apiKey, key: gemini-key}
    models: [{id: gemini-3.5-flash}]
"""


def test_embedder_signs_in_as_a_model_provider() -> None:
    config = parse_model_provider_yaml(PROVIDERS)

    embedder = OpenAIEmbedder.from_provider(config, model="text-embedding-3-large", dimensions=3072)

    assert embedder.url == "https://gateway.example/openai/v1/embeddings"
    assert embedder.headers == {"x-tenant": "forge"}
    assert (embedder.model, embedder.dimensions) == ("text-embedding-3-large", 3072)
    with pytest.raises(ModelProviderConfigError, match="OpenAI's API"):
        OpenAIEmbedder.from_provider(config, "google")
    with pytest.raises(ModelProviderConfigError, match="No model provider"):
        OpenAIEmbedder.from_provider(config, "azure")


# --- AtlasVectorMemoryService, on a real Atlas (Local) ------------------------

MONGO_URI = os.environ.get("FORGE_TEST_MONGO_URI") or os.environ.get("FORGE_MONGO_URI")


class WordsEmbedder:
    """Hashes each word into one of 64 dimensions: texts sharing words are near."""

    dimensions = 64

    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


@pytest.fixture
async def memory() -> AsyncIterator[AtlasVectorMemoryService]:
    if not MONGO_URI:
        pytest.skip("needs Atlas or Atlas Local: set FORGE_TEST_MONGO_URI or FORGE_MONGO_URI")
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        MONGO_URI, serverSelectionTimeoutMS=3000
    )
    try:
        await client.admin.command("ping")
    except Exception as error:  # noqa: BLE001
        await client.close()
        pytest.skip(f"MongoDB isn't reachable: {error}")
    database = f"forge_common_test_{uuid.uuid4().hex[:8]}"
    service = AtlasVectorMemoryService(
        client, database=database, embedder=WordsEmbedder(), max_results=3
    )
    try:
        async with asyncio.timeout(120):
            await service.wait_until_searchable()
        yield service
    finally:
        await client.drop_database(database)
        await client.close()


def _event(event_id: str, author: str, text: str | None) -> Event:
    content = types.Content(
        role="user" if author == "user" else "model", parts=[types.Part(text=text)]
    )
    return Event(id=event_id, invocation_id="inv", author=author, content=content if text else None)


def _session(session_id: str, user_id: str, events: list[Event]) -> Session:
    return Session(id=session_id, app_name="forge", user_id=user_id, events=events)


async def _eventually(check: Callable[[], Awaitable[list[str]]], top: str) -> list[str]:
    """Atlas Search syncs writes in about a second: retries until ``top`` ranks first."""
    deadline = time.monotonic() + 30
    while True:
        found = await check()
        if found[:1] == [top] or time.monotonic() > deadline:
            return found
        await asyncio.sleep(0.5)


def _texts(
    memory: AtlasVectorMemoryService, user_id: str, query: str
) -> Callable[[], Awaitable[list[str]]]:
    async def search() -> list[str]:
        response = await memory.search_memory(app_name="forge", user_id=user_id, query=query)
        return [
            part.text or "" for entry in response.memories for part in entry.content.parts or []
        ]

    return search


def _embedded(memory: AtlasVectorMemoryService) -> list[str]:
    embedder = memory.embedder
    assert isinstance(embedder, WordsEmbedder)
    return embedder.embedded


async def test_a_session_is_searched_by_meaning_for_its_user_only(
    memory: AtlasVectorMemoryService,
) -> None:
    await memory.add_session_to_memory(
        _session(
            "s1",
            "ada",
            [
                _event("e1", "user", "My dog is called Rex"),
                _event("e2", "helper", "Rex is a lovely name for a dog"),
                _event("e3", "user", None),  # no text: not kept
                _event("e4", "user", "I deploy on Fridays"),
            ],
        )
    )
    await memory.add_session_to_memory(
        _session("s2", "grace", [_event("g1", "user", "My dog is called Max")])
    )

    found = await _eventually(
        _texts(memory, "ada", "what is my dog called"), "My dog is called Rex"
    )
    assert found[0] == "My dog is called Rex"
    assert "My dog is called Max" not in found

    response = await memory.search_memory(app_name="forge", user_id="ada", query="dog called Rex")
    top = response.memories[0]
    assert (top.id, top.author) == ("e1", "user")
    assert top.timestamp
    assert top.custom_metadata["score"] > 0


async def test_adding_a_session_again_embeds_only_new_events_and_drops_removed_ones(
    memory: AtlasVectorMemoryService,
) -> None:
    first = [_event("e1", "user", "Deploys happen on Friday"), _event("e2", "helper", "Noted")]
    await memory.add_session_to_memory(_session("s1", "ada", first))
    later = [first[0], _event("e3", "user", "Actually deploys moved to Tuesday")]
    await memory.add_session_to_memory(_session("s1", "ada", later))

    assert _embedded(memory) == [
        "Deploys happen on Friday",
        "Noted",
        "Actually deploys moved to Tuesday",
    ]
    kept = await memory.collection.distinct("event_id", {"user_id": "ada"})
    assert sorted(kept) == ["e1", "e3"]


async def test_events_and_memories_are_added_once(memory: AtlasVectorMemoryService) -> None:
    turn = [_event("e1", "user", "I prefer metric units")]
    for _ in range(2):
        await memory.add_events_to_memory(
            app_name="forge", user_id="ada", events=turn, session_id="s1"
        )
    fact = MemoryEntry(
        id="units",
        author="user",
        content=types.Content(role="user", parts=[types.Part(text="Prefers kilometres")]),
    )
    await memory.add_memory(app_name="forge", user_id="ada", memories=[fact])
    revised = fact.model_copy(
        update={"content": types.Content(parts=[types.Part(text="Prefers miles now")])}
    )
    await memory.add_memory(app_name="forge", user_id="ada", memories=[revised])

    assert await memory.collection.count_documents({"user_id": "ada"}) == 2
    found = await _eventually(_texts(memory, "ada", "prefers miles now"), "Prefers miles now")
    assert found[0] == "Prefers miles now"


async def test_far_matches_are_dropped_under_min_score(memory: AtlasVectorMemoryService) -> None:
    await memory.add_session_to_memory(
        _session("s1", "ada", [_event("e1", "user", "My dog is called Rex")])
    )
    await _eventually(_texts(memory, "ada", "dog called Rex"), "My dog is called Rex")

    memory.min_score = 0.99
    assert await _texts(memory, "ada", "quarterly revenue forecast")() == []
    assert await _texts(memory, "ada", "   ")() == []
