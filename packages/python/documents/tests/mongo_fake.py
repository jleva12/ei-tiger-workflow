"""Async facade over mongomock so the Mongo stores' CRUD logic can be tested
without a server. ($search / $vectorSearch need real Atlas; see test_mongo.py
for how the pipeline builders are tested instead.)"""

from __future__ import annotations

from typing import Any

import mongomock


class AsyncCursor:
    def __init__(self, cursor: Any) -> None:
        self._c = cursor
        self._it: Any = None

    def sort(self, *a: Any, **k: Any) -> AsyncCursor:
        self._c = self._c.sort(*a, **k)
        return self

    def skip(self, n: int) -> AsyncCursor:
        self._c = self._c.skip(n)
        return self

    def limit(self, n: int) -> AsyncCursor:
        self._c = self._c.limit(n)
        return self

    def __aiter__(self) -> AsyncCursor:
        self._it = iter(self._c)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class AsyncCollection:
    def __init__(self, coll: Any) -> None:
        self._c = coll
        self.aggregate_calls: list[list[dict[str, Any]]] = []
        self.aggregate_results: Any = None  # callable(pipeline) -> list[dict]
        self.search_indexes: dict[str, dict[str, Any]] = {}  # name -> listSearchIndexes row
        self.search_index_updates: list[str] = []

    async def find_one(self, *a: Any, **k: Any) -> Any:
        return self._c.find_one(*a, **k)

    def find(self, *a: Any, **k: Any) -> AsyncCursor:
        return AsyncCursor(self._c.find(*a, **k))

    async def update_one(self, *a: Any, **k: Any) -> Any:
        return self._c.update_one(*a, **k)

    async def delete_one(self, *a: Any, **k: Any) -> Any:
        return self._c.delete_one(*a, **k)

    async def delete_many(self, *a: Any, **k: Any) -> Any:
        return self._c.delete_many(*a, **k)

    async def update_many(self, *a: Any, **k: Any) -> Any:
        return self._c.update_many(*a, **k)

    async def bulk_write(self, ops: list[Any], ordered: bool = True) -> Any:
        # mongomock's bulk API lags pymongo's; replay UpdateOne ops one by one
        from pymongo import ReplaceOne, UpdateOne
        from pymongo.errors import BulkWriteError, DuplicateKeyError

        errors = []
        for i, op in enumerate(ops):
            assert isinstance(op, (UpdateOne, ReplaceOne)), f"fake only supports UpdateOne/ReplaceOne, got {op!r}"
            try:
                if isinstance(op, ReplaceOne):
                    self._c.replace_one(op._filter, op._doc, upsert=bool(op._upsert))
                else:
                    self._c.update_one(op._filter, op._doc, upsert=bool(op._upsert))
            except DuplicateKeyError as exc:
                errors.append({"index": i, "code": 11000, "errmsg": str(exc)})
                if ordered:
                    break
        if errors:  # same shape as the real driver
            raise BulkWriteError({"writeErrors": errors, "writeConcernErrors": [], "nInserted": 0})
        return None

    async def find_one_and_update(self, *a: Any, **k: Any) -> Any:
        return self._c.find_one_and_update(*a, **k)

    async def create_indexes(self, models: list[Any]) -> list[str]:
        return [
            self._c.create_index(
                m.document["key"].items() if hasattr(m.document["key"], "items") else m.document["key"]
            )
            for m in models
        ]

    async def list_search_indexes(self) -> AsyncCursor:
        return AsyncCursor(iter([dict(v) for v in self.search_indexes.values()]))

    async def create_search_indexes(self, models: list[Any]) -> list[str]:
        for m in models:
            d = m.document
            self.search_indexes[d["name"]] = {
                "name": d["name"],
                "type": d.get("type", "search"),
                "status": "READY",
                "queryable": True,
                "latestDefinition": d["definition"],
            }
        return [m.document["name"] for m in models]

    async def update_search_index(self, name: str, definition: dict[str, Any]) -> None:
        self.search_index_updates.append(name)
        self.search_indexes[name]["latestDefinition"] = definition

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> AsyncCursor:
        self.aggregate_calls.append(pipeline)
        rows = self.aggregate_results(pipeline) if self.aggregate_results else []
        return AsyncCursor(iter(rows))


class AsyncDatabase:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._colls: dict[str, AsyncCollection] = {}

    def __getitem__(self, name: str) -> AsyncCollection:
        if name not in self._colls:
            self._colls[name] = AsyncCollection(self._db[name])
        return self._colls[name]


class AsyncMongoMockClient:
    def __init__(self) -> None:
        self._client = mongomock.MongoClient(tz_aware=True)
        self._dbs: dict[str, AsyncDatabase] = {}
        self.closed = False

    def __getitem__(self, name: str) -> AsyncDatabase:
        if name not in self._dbs:
            self._dbs[name] = AsyncDatabase(self._client[name])
        return self._dbs[name]

    async def close(self) -> None:
        self.closed = True
