"""ADK memory in MongoDB Atlas, searched by meaning. Needs ``forge-common[adk-memory]``.

:class:`AtlasVectorMemoryService` is ADK's ``InMemoryMemoryService`` kept in a
MongoDB collection instead of a dict, and searched with Atlas Vector Search
instead of shared words. Each event (or memory written directly) with text is
one document: its text, its content, who wrote it and when, and the text's
embedding. A search embeds the query and asks ``$vectorSearch`` for the
nearest documents of that app and user::

    config = load_model_provider_config(path)
    memory = AtlasVectorMemoryService.from_uri(
        settings.mongo_uri,
        database="forge_admin",
        embedder=OpenAIEmbedder.from_provider(config, "openai"),
    )
    runner = Runner(app=app, session_service=sessions, memory_service=memory)

:class:`OpenAIEmbedder` makes the embeddings with OpenAI's embeddings API,
signed in as model_provider.yaml's OpenAI provider is, so the agents' own key
(or gateway) serves them too. Atlas only keeps and searches the vectors, so it
works on any Atlas cluster and on the ``mongodb-atlas-local`` image, and a
memory is searchable as soon as Atlas Search has synced it (about a second).

The collection and its vector index are made on first use, so a service can be
built while Mongo is down. A new index takes a few seconds to build; searches
find nothing until it's ready.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx
from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types
from pymongo import ASCENDING, AsyncMongoClient, IndexModel, ReplaceOne
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import CollectionInvalid
from pymongo.operations import SearchIndexModel

from forge_common.model_provider import (
    ModelProviderConfig,
    ModelProviderConfigError,
    OAuth2Auth,
    OAuth2TokenSource,
)

if TYPE_CHECKING:
    from google.adk.events.event import Event
    from google.adk.sessions.session import Session

__all__ = [
    "OPENAI_BASE_URL",
    "AtlasVectorMemoryService",
    "Embedder",
    "InputType",
    "OpenAIEmbedder",
]

log = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"

# Whether a text is being kept or searched for. Some models embed the two a
# little differently, so each finds the other; OpenAI's embed both the same.
InputType = Literal["document", "query"]

# Where events added without a session go, as in ADK's InMemoryMemoryService.
_UNKNOWN_SESSION_ID = "__unknown_session_id__"


class Embedder(Protocol):
    """Turns texts into vectors of ``dimensions`` numbers each."""

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]: ...


class OpenAIEmbedder:
    """Embeddings from OpenAI's embeddings API (``POST {base_url}/embeddings``),
    or from any gateway that speaks it.

    :param api_key: Sent as a bearer token. Pass ``token`` instead for one
        that changes, such as an OAuth2 access token.
    :param token: Gives the bearer token to send with each request.
    :param model: ``text-embedding-3-small`` is cheap and good;
        ``text-embedding-3-large`` (3,072 dimensions) is better.
    :param dimensions: The vectors' length; the text-embedding-3 models can
        shorten theirs. The vector index is made for this many, so changing it
        means a new collection or index.
    :param base_url: The API, up to and including ``/v1``.
    :param headers: Sent with every request, such as a gateway's.
    :param max_chars: Each text is cut to this many characters first: the
        models take 8,191 tokens each, and a character is at most about one.
    :param batch_size: The most texts in one request (OpenAI takes 2,048).
    :param batch_chars: The most characters in one request, which keeps it
        under OpenAI's 300,000 tokens per request.
    :param client: An HTTP client to send with; one is made (and closed by
        :meth:`aclose`) otherwise.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        token: Callable[[], Awaitable[str]] | None = None,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        base_url: str = OPENAI_BASE_URL,
        headers: Mapping[str, str] | None = None,
        max_chars: int = 8_000,
        batch_size: int = 128,
        batch_chars: int = 280_000,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if bool(api_key) == (token is not None):
            raise ValueError("OpenAIEmbedder needs an API key or a token, and not both.")
        self._api_key = api_key
        self._token = token
        self.model = model
        self._dimensions = dimensions
        self.url = f"{base_url.rstrip('/')}/embeddings"
        self.headers = dict(headers or {})
        self.max_chars = max_chars
        self.batch_size = batch_size
        self.batch_chars = batch_chars
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_provider(
        cls, config: ModelProviderConfig, provider: str | None = None, **options: Any
    ) -> "OpenAIEmbedder":
        """An embedder signed in as one of model_provider.yaml's providers is:
        at its base URL, with its headers and its key or OAuth2 token.

        :param provider: The provider's ID; the default model's when omitted.
        :param options: The rest of the embedder's, such as ``model``.
        :raises ModelProviderConfigError: There's no such provider, or it
            doesn't speak OpenAI's API.
        """
        provider_id = provider or config.default.provider
        settings = config.providers.get(provider_id)
        if settings is None:
            raise ModelProviderConfigError(f"No model provider {provider_id!r} to embed with.")
        if settings.api not in ("openai-responses", "openai-completions"):
            raise ModelProviderConfigError(
                f"Model provider {provider_id!r} speaks {settings.api};"
                " embeddings need OpenAI's API."
            )
        auth = settings.auth
        credentials: dict[str, Any] = (
            {"token": OAuth2TokenSource(auth).token}
            if isinstance(auth, OAuth2Auth)
            else {"api_key": auth.key.get_secret_value()}
        )
        return cls(
            **credentials,
            base_url=str(settings.base_url),
            headers=settings.headers,
            **options,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch: list[str] = []
        size = 0
        for text in texts:
            text = text[: self.max_chars]
            if batch and (len(batch) == self.batch_size or size + len(text) > self.batch_chars):
                vectors.extend(await self._embed_batch(batch))
                batch, size = [], 0
            batch.append(text)
            size += len(text)
        if batch:
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        bearer = self._api_key if self._token is None else await self._token()
        response = await self._client.post(
            self.url,
            headers={**self.headers, "Authorization": f"Bearer {bearer}"},
            json={
                "input": texts,
                "model": self.model,
                "dimensions": self._dimensions,
                "encoding_format": "float",
            },
        )
        if response.is_error:
            # The body says why (a bad key, a rate limit, an unknown model), never the key.
            raise httpx.HTTPStatusError(
                f"OpenAI embeddings answered {response.status_code}: {response.text[:500]}",
                request=response.request,
                response=response,
            )
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        if len(data) != len(texts):
            raise ValueError(
                f"OpenAI embeddings returned {len(data)} vectors for {len(texts)} texts."
            )
        return [item["embedding"] for item in data]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class AtlasVectorMemoryService(BaseMemoryService):
    """ADK memory in a MongoDB collection, searched with Atlas Vector Search.

    Memories are kept per app and user, as ADK scopes them. Adding a session
    replaces what was kept of it (events it no longer has are dropped); adding
    events appends those not kept yet; adding memories replaces any with the
    same ID. Only new text is embedded, so adding a session again after each
    turn embeds just that turn.

    :param client: The Mongo client; :meth:`aclose` leaves it open.
    :param database: The database the collection is in.
    :param embedder: Embeds what's kept and what's searched for, usually an
        :class:`OpenAIEmbedder`.
    :param collection: The collection memories are kept in.
    :param index_name: The name of its vector search index.
    :param similarity: How the index compares vectors. OpenAI's are unit
        length, so ``cosine`` and ``dotProduct`` rank alike.
    :param max_results: The most memories a search returns.
    :param min_score: Drop matches scoring under this (0 to 1 for cosine);
        ``None`` keeps the ``max_results`` nearest, however far.
    """

    def __init__(
        self,
        client: AsyncMongoClient[dict[str, Any]],
        *,
        database: str,
        embedder: Embedder,
        collection: str = "adk_memories",
        index_name: str = "adk_memories_vector",
        similarity: Literal["cosine", "dotProduct", "euclidean"] = "cosine",
        max_results: int = 10,
        min_score: float | None = None,
    ) -> None:
        self._client = client
        self._owns_client = False
        self._database = database
        self._collection_name = collection
        self.embedder = embedder
        self.index_name = index_name
        self.similarity = similarity
        self.max_results = max_results
        self.min_score = min_score
        self._ready_lock = asyncio.Lock()
        self._is_ready = False

    @classmethod
    def from_uri(
        cls,
        uri: str,
        *,
        database: str,
        embedder: Embedder,
        timeout: float = 5.0,
        **options: Any,
    ) -> "AtlasVectorMemoryService":
        """A service with a client of its own, which :meth:`aclose` closes."""
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            uri,
            appname="forge-adk-memory",
            tz_aware=True,
            serverSelectionTimeoutMS=int(timeout * 1000),
            connectTimeoutMS=int(timeout * 1000),
        )
        service = cls(client, database=database, embedder=embedder, **options)
        service._owns_client = True
        return service

    @property
    def collection(self) -> AsyncCollection[dict[str, Any]]:
        return self._client[self._database][self._collection_name]

    async def aclose(self) -> None:
        """Closes the client if the service made it, and the embedder if it can be."""
        if self._owns_client:
            await self._client.close()
        close = getattr(self.embedder, "aclose", None)
        if close is not None:
            await close()

    # --- Adding ---------------------------------------------------------------

    async def add_session_to_memory(self, session: "Session") -> None:
        scope = {
            "app_name": session.app_name,
            "user_id": session.user_id,
            "session_id": session.id,
            "kind": "event",
        }
        events = [event for event in session.events if _text_of(event.content)]
        await self._ready()
        # The session as it is now replaces what was kept of it.
        await self.collection.delete_many(
            {**scope, "event_id": {"$nin": [event.id for event in events]}}
        )
        await self._add_events(scope, events)

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence["Event"],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        scope = {
            "app_name": app_name,
            "user_id": user_id,
            "session_id": session_id or _UNKNOWN_SESSION_ID,
            "kind": "event",
        }
        await self._ready()
        await self._add_events(
            scope,
            [event for event in events if _text_of(event.content)],
            dict(custom_metadata or {}),
        )

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        entries = [entry for entry in memories if _text_of(entry.content)]
        if not entries:
            return
        await self._ready()
        vectors = await self.embedder.embed(
            [_text_of(entry.content) for entry in entries], input_type="document"
        )
        now = datetime.now(UTC)
        writes = []
        for entry, vector in zip(entries, vectors, strict=True):
            memory_id = entry.id or uuid.uuid4().hex
            document = {
                "_id": f"memory:{app_name}:{user_id}:{memory_id}",
                "app_name": app_name,
                "user_id": user_id,
                "session_id": None,
                "kind": "memory",
                "memory_id": memory_id,
                "author": entry.author,
                "timestamp": entry.timestamp or now.isoformat(),
                "text": _text_of(entry.content),
                "content": _dump(entry.content),
                "custom_metadata": {**(custom_metadata or {}), **entry.custom_metadata},
                "embedding": vector,
                "created_at": now,
            }
            writes.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
        await self.collection.bulk_write(writes, ordered=False)

    async def _add_events(
        self,
        scope: dict[str, Any],
        events: Sequence["Event"],
        custom_metadata: dict[str, object] | None = None,
    ) -> None:
        """Keeps the events not kept yet, embedding only those."""
        if not events:
            return
        kept = {
            document["event_id"]
            async for document in self.collection.find(
                {**scope, "event_id": {"$in": [event.id for event in events]}}, {"event_id": 1}
            )
        }
        new: dict[str, Event] = {}
        for event in events:
            if event.id not in kept:
                new.setdefault(event.id, event)
        if not new:
            return
        vectors = await self.embedder.embed(
            [_text_of(event.content) for event in new.values()], input_type="document"
        )
        now = datetime.now(UTC)
        key = f"{scope['app_name']}:{scope['user_id']}:{scope['session_id']}"
        documents = [
            {
                "_id": f"event:{key}:{event.id}",
                **scope,
                "event_id": event.id,
                "author": event.author,
                "timestamp": datetime.fromtimestamp(event.timestamp).isoformat(),
                "text": _text_of(event.content),
                "content": _dump(event.content),
                "custom_metadata": custom_metadata or {},
                "embedding": vector,
                "created_at": now,
            }
            for event, vector in zip(new.values(), vectors, strict=True)
        ]
        # Upserts, so two writers adding the same turn at once both succeed.
        await self.collection.bulk_write(
            [ReplaceOne({"_id": document["_id"]}, document, upsert=True) for document in documents],
            ordered=False,
        )

    # --- Searching ------------------------------------------------------------

    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        if not query.strip():
            return SearchMemoryResponse()
        await self._ready()
        [vector] = await self.embedder.embed([query], input_type="query")
        pipeline: list[dict[str, Any]] = [
            {
                "$vectorSearch": {
                    "index": self.index_name,
                    "path": "embedding",
                    "queryVector": vector,
                    # Atlas recommends weighing about 20 candidates per result.
                    "numCandidates": min(max(self.max_results * 20, 100), 10_000),
                    "limit": self.max_results,
                    "filter": {"app_name": app_name, "user_id": user_id},
                }
            },
            {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
        ]
        if self.min_score is not None:
            pipeline.append({"$match": {"score": {"$gte": self.min_score}}})
        cursor = await self.collection.aggregate(pipeline)
        memories = [
            MemoryEntry(
                id=document.get("memory_id") or document.get("event_id"),
                content=types.Content.model_validate(document["content"]),
                author=document.get("author"),
                timestamp=document.get("timestamp"),
                custom_metadata={**document.get("custom_metadata", {}), "score": document["score"]},
            )
            async for document in cursor
        ]
        return SearchMemoryResponse(memories=memories)

    # --- The collection and its indexes ---------------------------------------

    async def _ready(self) -> None:
        """Makes the collection, its index and its vector index, once per service."""
        if self._is_ready:
            return
        async with self._ready_lock:
            if self._is_ready:
                return
            database = self._client[self._database]
            with contextlib.suppress(CollectionInvalid):  # It's there already.
                await database.create_collection(self._collection_name)
            await self.collection.create_indexes(
                [
                    IndexModel(
                        [("app_name", ASCENDING), ("user_id", ASCENDING), ("session_id", ASCENDING)]
                    )
                ]
            )
            await self._ensure_vector_index()
            self._is_ready = True

    async def _ensure_vector_index(self) -> None:
        cursor = await self.collection.list_search_indexes(self.index_name)
        existing = await cursor.to_list()
        if existing:
            fields = existing[0].get("latestDefinition", {}).get("fields", [])
            dimensions = next(
                (f.get("numDimensions") for f in fields if f.get("type") == "vector"), None
            )
            if dimensions not in (None, self.embedder.dimensions):
                log.warning(
                    "adk memory: %s.%s's index %s is for %s dimensions, but the embedder makes %s;"
                    " searches will fail until one of them changes",
                    self._database,
                    self._collection_name,
                    self.index_name,
                    dimensions,
                    self.embedder.dimensions,
                )
            return
        await self.collection.create_search_index(
            SearchIndexModel(
                name=self.index_name,
                type="vectorSearch",
                definition={
                    "fields": [
                        {
                            "type": "vector",
                            "path": "embedding",
                            "numDimensions": self.embedder.dimensions,
                            "similarity": self.similarity,
                        },
                        {"type": "filter", "path": "app_name"},
                        {"type": "filter", "path": "user_id"},
                    ]
                },
            )
        )
        log.info(
            "adk memory: made vector index %s on %s.%s",
            self.index_name,
            self._database,
            self._collection_name,
        )

    async def wait_until_searchable(self) -> None:
        """Waits for the vector index to be ready to query, for tests and set-up
        scripts. Bound it with ``asyncio.timeout``: it waits as long as it takes.
        """
        await self._ready()
        while True:
            cursor = await self.collection.list_search_indexes(self.index_name)
            indexes = await cursor.to_list()
            if indexes and indexes[0].get("queryable"):
                return
            await asyncio.sleep(0.5)


def _text_of(content: types.Content | None) -> str:
    """The content's text parts, as one string; empty when it has none."""
    if not content or not content.parts:
        return ""
    return " ".join(part.text for part in content.parts if part.text).strip()


def _dump(content: types.Content | None) -> dict[str, Any]:
    if content is None:
        return {}
    return content.model_dump(mode="json", exclude_none=True)
