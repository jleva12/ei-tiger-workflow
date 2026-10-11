"""A code graph database on the Spanner emulator, made with the worker's own
schema and written as the worker's loader writes it: records, their search
documents and embeddings, retained source, runs.

The worker's Go tests built these graphs with its loader; this writes the
same rows directly, so the reader's SQL runs against the real indexes
(search, vector, name, edge) without the Go toolchain. Only for tests: the
worker writes every graph there is.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.api_core import exceptions as google_errors
from google.cloud import spanner

from forge_codegraph_query import codesearch
from forge_codegraph_query.model import graph_id

#: The worker's DDL, which every reader of the graph depends on.
SCHEMA = Path(__file__).resolve().parents[4] / "packages/go/code-graph/storage/spanner/schema"
PROJECT = "codegraph-test"
INSTANCE = "reader-tests"
VECTOR_LENGTH = 4
MODEL = "fake"
ACCEPTED = datetime(2026, 10, 1, tzinfo=UTC)


def emulator() -> str | None:
    return os.environ.get("SPANNER_EMULATOR_HOST") or None


def schema(vector_length: int) -> list[str]:
    """The worker's DDL statements, in file order."""
    statements: list[str] = []
    for path in sorted(SCHEMA.glob("*.sql")):
        text = path.read_text().replace("{{VECTOR_LENGTH}}", str(vector_length))
        statements += [s.strip() for s in text.split(";") if s.strip()]
    return statements


def create_database() -> str:
    """A fresh database with the worker's schema; its full name."""
    client = spanner.Client(project=PROJECT)
    instance = client.instance(
        INSTANCE,
        configuration_name=f"projects/{PROJECT}/instanceConfigs/emulator-config",
        node_count=1,
    )
    with contextlib.suppress(google_errors.AlreadyExists):
        instance.create().result(60)
    name = f"t{uuid.uuid4().hex[:20]}"
    instance.database(name, ddl_statements=schema(VECTOR_LENGTH)).create().result(120)
    return f"projects/{PROJECT}/instances/{INSTANCE}/databases/{name}"


def drop_database(database: str) -> None:
    _, project, _, instance, _, name = database.split("/")
    spanner.Client(project=project).instance(instance).database(name).drop()


def _b64(data: bytes) -> bytes:
    # The Python client takes BYTES cells base64-encoded.
    return base64.b64encode(data)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class Graph:
    """One repository's graph, written as one transaction by :meth:`write`."""

    url: str
    branch: str = "main"
    live: int = 1
    commit: str = "c" * 40
    repository: str = ""
    rows: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    # One search document and embedding per node, as the loader keeps them:
    # the latest version's.
    documents: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    embeddings: dict[str, tuple[Any, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repository = self.repository or graph_id("repo", self.url.lower())

    def _add(self, table: str, row: tuple[Any, ...]) -> None:
        self.rows.setdefault(table, []).append(row)

    def node(
        self,
        kind: str,
        name: str,
        qualified: str,
        path: str,
        *,
        props: dict[str, str] | None = None,
        source: str = "",
        start_line: int = 1,
        gen_from: int = 1,
        gen_to: int | None = None,
        retired: bool = False,
        vector: list[float] | None = None,
        document: bool = True,
        node_id: str = "",
    ) -> dict[str, Any]:
        """A version of a node open from ``gen_from`` (to ``gen_to``, which
        ``retired`` it), with its search document (and embedding) as the
        loader writes them, and its file's text as retained source. Another
        version of the same node (``node_id``) replaces its document."""
        properties = {"file_path": {"string": path}}
        for key, value in (props or {}).items():
            properties[key] = {"string": value}
        node: dict[str, Any] = {
            "id": node_id or graph_id(kind, self.repository, qualified),
            "kind": kind,
            "name": name,
            "qualified_name": qualified,
            "properties": properties,
        }
        lineage = graph_id("file", self.repository, path)
        if source:
            data = source.encode()
            sha = hashlib.sha256(data).hexdigest()
            node["source"] = {
                "lineage": lineage,
                "content_sha256": sha,
                "span": {
                    "start": {"byte_offset": 0, "line": start_line, "column": 1},
                    "end": {
                        "byte_offset": len(data),
                        "line": start_line + source.count("\n"),
                        "column": 1,
                    },
                },
            }
            self._add("CGContent", (self.repository, sha, len(data), _b64(gzip.compress(data))))
        fact = json.dumps({"node": node}, separators=(",", ":")).encode()
        found = codesearch.document_of(node) if document else None
        self._add(
            "CGRecords",
            (
                self.repository, "node", node["id"], gen_from, gen_to, f"c{gen_from}",
                f"c{gen_to}" if gen_to else "", retired, lineage, kind, name, None, None,
                _digest(fact), found.hash if found else None, _b64(fact),
            ),
        )  # fmt: skip
        if found is not None:
            self.documents[node["id"]] = (
                    self.repository, node["id"], gen_from, kind, name, name.lower(), qualified,
                    path, found.hash, codesearch.DOCUMENT_VERSION, found.text,
                    codesearch.identifier_terms(name, qualified),
            )  # fmt: skip
            if vector is not None:
                self.embeddings[node["id"]] = (
                    self.repository, MODEL, VECTOR_LENGTH, node["id"], found.hash,
                    codesearch.DOCUMENT_VERSION, found.text, vector, gen_from,
                )  # fmt: skip
        return node

    def embed(self, node: dict[str, Any], vector: list[float]) -> None:
        """The node's current document embedded as ``vector``."""
        document = self.documents[node["id"]]
        self.embeddings[node["id"]] = (
            self.repository, MODEL, VECTOR_LENGTH, node["id"], document[8],
            codesearch.DOCUMENT_VERSION, document[10], vector, document[2],
        )  # fmt: skip

    def edge(
        self, kind: str, source: dict[str, Any], target: dict[str, Any], *, gen_from: int = 1
    ) -> dict[str, Any]:
        edge = {
            "id": graph_id("edge", kind, source["id"], target["id"]),
            "kind": kind,
            "source_id": source["id"],
            "target_id": target["id"],
        }
        fact = json.dumps({"edge": edge}, separators=(",", ":")).encode()
        self._add(
            "CGRecords",
            (
                self.repository, "edge", edge["id"], gen_from, None, "c1", "", False, None,
                kind, "", source["id"], target["id"], _digest(fact), None, _b64(fact),
            ),
        )  # fmt: skip
        return edge

    def run(
        self, run_id: str, sequence: int, *, phase: str = "SUCCEEDED", generation: int = 1
    ) -> None:
        payload = {
            "schema_version": 1,
            "key": {"repository_id": self.repository, "run_id": run_id},
            "request": {"deployment_sequence": sequence, "branch": self.branch},
            "phase": phase,
            "generation": generation,
        }
        data = json.dumps(payload).encode()
        self._add(
            "CGRuns",
            (
                self.repository, run_id, f"d{sequence}", sequence, f"{sequence:040d}",
                "sha256:" + "0" * 64, "sha256:" + hashlib.sha256(run_id.encode()).hexdigest(),
                phase, generation, 1, ACCEPTED, ACCEPTED, _b64(data),
            ),
        )  # fmt: skip

    def write(self, database: str) -> None:
        registration = json.dumps({"github_url": self.url, "branch": self.branch}).encode()
        repository = (self.repository, self.live, self.commit, "run-live", "", "", 0, None, 1)
        tables = [
            ("CGRepositories", [(*repository, _b64(registration))]),
            *self.rows.items(),
            ("CGSearchDocuments", list(self.documents.values())),
            ("CGSearchEmbeddings", list(self.embeddings.values())),
        ]
        _, project, _, instance, _, name = database.split("/")
        db = spanner.Client(project=project).instance(instance).database(name)
        with db.batch() as batch:
            for table, rows in tables:
                if rows:
                    batch.insert(table, COLUMNS[table], rows)


COLUMNS: dict[str, Iterable[str]] = {
    "CGRepositories": (
        "RepositoryID", "LiveGeneration", "LiveCommit", "LiveRunID", "LeaseOwner",
        "LeaseRunID", "LeaseToken", "LeaseExpiresAt", "Revision", "Payload",
    ),
    "CGRecords": (
        "RepositoryID", "RecordKind", "RecordID", "GenFrom", "GenTo", "CommitFrom",
        "CommitTo", "Retired", "Lineage", "Kind", "Name", "SourceID", "TargetID",
        "FactDigest", "SearchHash", "Payload",
    ),
    "CGContent": ("RepositoryID", "SHA256", "UncompressedBytes", "Payload"),
    "CGSearchDocuments": (
        "RepositoryID", "NodeID", "Generation", "Kind", "Name", "NameLower", "QualifiedName",
        "FilePath", "DocumentHash", "DocumentVersion", "DocumentText", "Identifiers",
    ),
    "CGSearchEmbeddings": (
        "RepositoryID", "Model", "Dimensions", "NodeID", "DocumentHash", "DocumentVersion",
        "DocumentText", "Embedding", "UpdatedGeneration",
    ),
    "CGRuns": (
        "RepositoryID", "RunID", "DeploymentID", "DeploymentSequence", "CommitSHA",
        "ConfigDigest", "IdentityDigest", "Phase", "Generation", "Revision", "AcceptedAt",
        "UpdatedAt", "Payload",
    ),
}  # fmt: skip
