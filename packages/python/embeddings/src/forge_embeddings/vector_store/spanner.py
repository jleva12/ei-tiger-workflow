"""Spanner primitives, shared schema and native hybrid search.

All SDK I/O runs outside the event loop. Transactions protect read/modify/write
operations; embeddings are separate from JSON so ordinary reads never fetch them.
Exact KNN deliberately supports mixed model dimensions without rebuilding an ANN
index. Model and dimension filters are evaluated before distance functions.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from importlib.resources import files
from typing import Any

from google.api_core import exceptions as google_errors
from google.api_core.retry import Retry
from google.cloud import spanner
from google.cloud.spanner_v1 import param_types
from google.cloud.spanner_v1.data_types import JsonObject
from google.cloud.spanner_v1.pool import BurstyPool
from pydantic import BaseModel

from forge_embeddings.fusion import rrf_fuse
from forge_embeddings.vector_store import spanner_database
from forge_tasks.errors import StorageError, TaskError

RECORDS = "ForgeVectorRecords"
VECTORS = "ForgeVectors"


def types(params: Mapping[str, Any]) -> dict[str, Any]:
    def infer(value: Any) -> Any:
        if isinstance(value, bool):
            return param_types.BOOL
        if isinstance(value, int):
            return param_types.INT64
        if isinstance(value, float):
            return param_types.FLOAT64
        if isinstance(value, datetime):
            return param_types.TIMESTAMP
        if isinstance(value, (list, tuple)):
            return param_types.Array(infer(value[0]) if value else param_types.STRING)
        return param_types.STRING

    return {k: param_types.Array(param_types.FLOAT64) if k == "vector" else infer(v) for k, v in params.items()}


def execute(reader: Any, sql: str, params: dict[str, Any]) -> list[Any]:
    return list(reader.execute_sql(sql, params=params, param_types=types(params), timeout=30, retry=Retry(timeout=60)))


def payload(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def model_json(model: BaseModel) -> JsonObject:
    return JsonObject(model.model_dump(mode="json", exclude={"embedding"}))


def get_record(tx: Any, namespace: str, tenant: str, key: str) -> dict[str, Any] | None:
    rows = execute(
        tx,
        f"SELECT Payload FROM {RECORDS} WHERE Namespace=@ns AND TenantId=@tenant AND Id=@id",
        {"ns": namespace, "tenant": tenant, "id": key},
    )
    return payload(rows[0][0]) if rows else None


def put_record(tx: Any, namespace: str, tenant: str, key: str, model: BaseModel) -> None:
    tx.insert_or_update(
        RECORDS, ["Namespace", "TenantId", "Id", "Payload"], [[namespace, tenant, key, model_json(model)]]
    )


def vector_row(model: Any, *, namespace: str, scope: str, key: str, seq: int = 0) -> dict[str, Any]:
    vector = model.embedding
    if vector is not None and (not vector or not all(math.isfinite(x) for x in vector)):
        raise ValueError("embedding must contain finite values and at least one dimension")
    return dict(
        Namespace=namespace,
        TenantId=model.tenant_id,
        ScopeId=scope,
        Id=key,
        Payload=model_json(model),
        RunSeq=seq,
        ContentHash=model.content_hash,
        EmbeddingModel=model.embedding_model,
        Embedding=[float(v) for v in vector] if vector is not None else None,
        SearchText=model.text if namespace == "chunks" else model.keyword_text,
        Title=model.title,
        SectionText=model.section_text if namespace == "chunks" else "",
        ContextText=(model.context or "") if namespace == "chunks" else "",
        Identifiers=sorted({s.lower() for s in (model.identifiers if namespace == "chunks" else model.symbols)}),
    )


def put_vectors(tx: Any, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        columns = list(rows[0])
        tx.insert_or_update(VECTORS, columns, [[r[c] for c in columns] for r in rows])


async def storage_io(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Keep the worker's storage retry semantics identical across providers."""
    try:
        return await asyncio.to_thread(callback, *args, **kwargs)
    except (google_errors.GoogleAPICallError, google_errors.RetryError) as exc:
        transient = isinstance(
            exc,
            (
                google_errors.Aborted,
                google_errors.DeadlineExceeded,
                google_errors.ServiceUnavailable,
                google_errors.InternalServerError,
                google_errors.ResourceExhausted,
                google_errors.TooManyRequests,
                google_errors.RetryError,
            ),
        )
        error = StorageError if transient else TaskError
        raise error(f"spanner: {exc}") from exc


class Database:
    def __init__(self, database: Any = None) -> None:
        self._owns = database is None
        if database is None:
            # Compose/cloud overlays use an empty value to disable the emulator.
            # The Python SDK otherwise treats an empty string as a real endpoint.
            if os.environ.get("SPANNER_EMULATOR_HOST") == "":
                os.environ.pop("SPANNER_EMULATOR_HOST")
            _, project, _, instance, _, name = spanner_database().split("/")
            # SDK 3.70/3.71's multiplexed maintenance thread sleeps for ten
            # minutes and close() joins it. Use the supported pooled mode by
            # default so worker/CLI shutdown remains prompt. Explicit SDK env
            # settings still win.
            for option in (
                "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS",
                "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS_FOR_RW",
                "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS_PARTITIONED_OPS",
            ):
                os.environ.setdefault(option, "false")
            credentials = None
            raw_credentials = os.environ.get("FORGE_GOOGLE_CREDENTIALS_JSON")
            if raw_credentials:
                from google.oauth2 import service_account

                try:
                    info = json.loads(raw_credentials)
                    if not isinstance(info, dict) or info.get("type") != "service_account":
                        raise ValueError("expected service account")
                    credentials = service_account.Credentials.from_service_account_info(info)
                except (ValueError, KeyError, TypeError):
                    raise ValueError(
                        "FORGE_GOOGLE_CREDENTIALS_JSON must contain valid service-account credentials"
                    ) from None
            self.client = spanner.Client(project=project, credentials=credentials)
            self.pool = BurstyPool()
            database = self.client.instance(instance).database(name, pool=self.pool)
        self.db = database

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        def run() -> list[Any]:
            with self.db.snapshot() as snapshot:
                return execute(snapshot, sql, params or {})

        return await storage_io(run)

    async def transaction(self, callback: Callable[..., Any]) -> Any:
        return await storage_io(self.db.run_in_transaction, callback, timeout_secs=60)

    async def delete(self, table: str, where: str, params: dict[str, Any]) -> int:
        return await self.transaction(
            lambda tx: tx.execute_update(f"DELETE FROM {table} WHERE {where}", params=params, param_types=types(params))
        )

    async def ensure_schema(self, *, embedding_dimensions: int) -> None:
        if embedding_dimensions < 1:
            raise ValueError("embedding_dimensions must be positive")
        # Check every object, including indexes after an interrupted DDL operation.
        ddl = files(__package__).joinpath("schema.sql").read_text()
        ddl = re.sub(r"--[^\n]*", "", ddl)
        for statement in (s.strip() for s in ddl.split(";") if s.strip()):
            match = re.match(r"CREATE (?:SEARCH )?(TABLE|INDEX) (\w+)", statement)
            assert match
            kind, name = match.groups()
            source, column = ("TABLES", "TABLE_NAME") if kind == "TABLE" else ("INDEXES", "INDEX_NAME")
            existing = await self.query(
                f"SELECT {column} FROM INFORMATION_SCHEMA.{source} WHERE {column}=@name", {"name": name}
            )
            if not existing:
                from google.api_core.exceptions import AlreadyExists, FailedPrecondition

                try:
                    await asyncio.to_thread(self._apply_ddl, statement)
                except (AlreadyExists, FailedPrecondition):
                    # Only tolerate a concurrent initializer if the object is
                    # now present. Invalid DDL and permission failures surface.
                    if not await self.query(
                        f"SELECT {column} FROM INFORMATION_SCHEMA.{source} WHERE {column}=@name", {"name": name}
                    ):
                        raise

    def _apply_ddl(self, statement: str) -> None:
        self.db.update_ddl([statement]).result(timeout=600)

    async def close(self) -> None:
        if self._owns:
            await asyncio.to_thread(self.db.close)
            await asyncio.to_thread(self.pool.clear)
            self.db.spanner_api.transport.close()

    async def embeddings(self, namespace: str, tenant: str, hashes: Sequence[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}
        rows = await self.query(
            f"SELECT ContentHash, Embedding FROM {VECTORS} WHERE Namespace=@ns AND TenantId=@tenant AND ContentHash IN UNNEST(@hashes) AND Embedding IS NOT NULL",
            {"ns": namespace, "tenant": tenant, "hashes": list(hashes)},
        )
        return {h: list(v) for h, v in rows}


# JSON field names below are source constants, never user input.
def field(name: str) -> str:
    return f"JSON_VALUE(Payload, '$.{name}')"


def array(name: str) -> str:
    return f"JSON_VALUE_ARRAY(Payload, '$.{name}')"


def intersect(expression: str, parameter: str) -> str:
    return f"EXISTS (SELECT 1 FROM UNNEST({expression}) AS item WHERE item IN UNNEST(@{parameter}))"


class Search:
    def __init__(self, db: Database, namespace: str, similarity: str = "dotProduct") -> None:
        if similarity not in {"dotProduct", "cosine", "euclidean"}:
            raise ValueError("unsupported vector similarity")
        self.db, self.namespace, self.similarity = db, namespace, similarity

    async def leg(
        self,
        kind: str,
        *,
        where: str,
        params: dict[str, Any],
        text: str,
        vector: list[float],
        model: str | None,
        identifiers: Sequence[str],
        limit: int,
    ) -> list[tuple[dict[str, Any], float]]:
        p = {**params, "ns": self.namespace, "limit": max(0, limit)}
        condition = f"Namespace=@ns AND ({where})"
        if kind == "vector":
            if not vector:
                return []
            if not all(math.isfinite(v) for v in vector):
                raise ValueError("query embedding must be finite")
            p.update(vector=[float(v) for v in vector], dimensions=len(vector))
            valid = "Embedding IS NOT NULL AND ARRAY_LENGTH(Embedding)=@dimensions"
            if model:
                p["model"] = model
                valid += " AND EmbeddingModel=@model"
            fn = {
                "dotProduct": "DOT_PRODUCT(Embedding,@vector)",
                "cosine": "1-COSINE_DISTANCE(Embedding,@vector)",
                "euclidean": "-EUCLIDEAN_DISTANCE(Embedding,@vector)",
            }[self.similarity]
            if self.similarity == "cosine":
                if not any(vector):
                    return []
                valid += " AND EXISTS (SELECT 1 FROM UNNEST(Embedding) AS v WHERE v != 0)"
            # CASE short circuits even if the optimizer moves predicates.
            score = f"CASE WHEN {valid} THEN {fn} ELSE NULL END"
            condition += f" AND {valid}"
        elif kind == "symbols":
            if not identifiers:
                return []
            p["identifiers"] = sorted({x.lower() for x in identifiers})
            score = "(SELECT COUNT(*) FROM UNNEST(Identifiers) AS item WHERE item IN UNNEST(@identifiers))"
            condition += " AND " + intersect("Identifiers", "identifiers")
        else:
            if not text.strip() and not identifiers:
                return []
            # Plain user text is a bag of terms, not Spanner search query syntax.
            words = re.findall(r"\w+", text, re.UNICODE)
            p["text"] = " OR ".join('"' + w + '"' for w in words)
            fields = {"TextTokens": 1, "TitleTokens": 1.5}
            if self.namespace == "chunks":
                fields.update(SectionTokens=2, ContextTokens=0.5)
            terms = [f"COALESCE(SCORE({f}, @text), 0)*{w}" for f, w in fields.items()] if words else ["0.0"]
            matches = [f"SEARCH({f}, @text)" for f in fields] if words else ["FALSE"]
            if self.namespace == "chunks" and len(words) > 1:
                p["phrase"] = '"' + " ".join(words) + '"'
                terms.append("IF(SEARCH(TextTokens, @phrase), 3.0, 0.0)")
            if self.namespace == "chunks" and identifiers:
                p["identifiers"] = sorted({x.lower() for x in identifiers})
                terms.append("5*(SELECT COUNT(*) FROM UNNEST(Identifiers) AS item WHERE item IN UNNEST(@identifiers))")
                matches.append(intersect("Identifiers", "identifiers"))
            score = "+".join(terms)
            condition += " AND (" + " OR ".join(matches) + ")"
        rows = await self.db.query(
            f"SELECT Payload, {score} AS Score FROM {VECTORS} WHERE {condition} ORDER BY Score DESC, TenantId, ScopeId, Id LIMIT @limit",
            p,
        )
        return [(payload(row[0]), float(row[1])) for row in rows]

    async def fused(
        self, *, weights: dict[str, float], limit: int, leg_limit: int, **kwargs: Any
    ) -> list[tuple[dict[str, Any], float, dict[str, int]]]:
        names = list(weights)
        results = await asyncio.gather(*(self.leg(name, limit=leg_limit, **kwargs) for name in names))

        def key(row: dict[str, Any]) -> str:
            return json.dumps([row["tenant_id"], row.get("repo_id", row.get("doc_id")), row.get("sha", row.get("id"))])

        by_id = {key(row): row for leg in results for row, _ in leg}
        fused = rrf_fuse(
            {name: [key(row) for row, _ in leg] for name, leg in zip(names, results, strict=True)}, weights
        )
        return [(by_id[k], score, ranks) for k, score, ranks in fused[:limit]]
