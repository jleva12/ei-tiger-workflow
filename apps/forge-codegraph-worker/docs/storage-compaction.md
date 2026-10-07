# Storage compaction

How to cut what a repository costs in Spanner without changing what the
graph says or slowing any read. Measured on google/adk-java (558 Java files,
139,621 lines, 5.4 MB of source) at generation 2 on the managed instance
`code-graph`: 815 MiB across tables and indexes, 1,088 MiB billed. Most
repositories are larger than this one, so the ratio matters more than the
absolute number: at 200 times the source, a 40,000-file monorepo lands near
70 GB and its initial load, already the slowest stage of a run, scales with
the bytes committed.

## Constraints

1. **No fact is lost or changed.** Every stored node and edge decodes to the
   same `graph.Fact` it was written from: ids, kinds, names, source anchors
   with their content hash and byte span, every property with its type, and
   the version history. Per-occurrence edges stay per-occurrence.
2. **No read gets slower.** Every reader keeps its index and its query shape.
   The only change a reader sees is fewer bytes per row.
3. **The analysis digest does not change.** Encoding is a storage concern.
   A compaction release must not force re-admission of every repository.
4. **`Fact.Digest()` does not change.** The differ compares the stored
   `FactDigest` with the digest of the fact it just projected
   ([differ.go:216](../internal/graphanalysis/differ.go#L216)).
   The digest is SHA-256 of the fact's canonical JSON, computed in memory,
   so the storage encoding may change freely but the Go struct's JSON shape
   may not. Adding a field to `graph.Node` or `graph.Edge` would turn the
   next incremental run into a full rewrite.
5. **Old rows stay readable forever.** History is immutable; a reader must
   decode every encoding ever written.

## Where the bytes are

Per-table physical size for adk-java, from
`SPANNER_SYS.TABLE_SIZES_STATS_1HOUR` once the load had settled:

| Component | MiB | Notes |
|---|---:|---|
| `CGRecords` | 345 | 325,650 rows, about 1.1 KiB each |
| Five secondary indexes on `CGRecords` | 250 | ByLineage 70, EdgesByTarget 54, EdgesBySource 54, ByKind 42, ByGenFrom 30 |
| `CGSearchEmbeddings` plus vector index | 110 | 11,628 rows at 3,072 float32 |
| `CGSearchDocuments` plus search index | 87 | substring token list alone is 35 |
| `CGFileIdentities` | 22 | 620 rows, about 18 KiB of JSON each |
| `CGContent` | 2 | gzip, already compact |

Inside a `CGRecords` row:

| Part | Bytes | What it is |
|---|---:|---|
| `Payload` | 578 for a `calls` edge, about 780 average for a node | `json.Marshal(graph.Fact)`: the id, kind, source and target ids repeated from the key columns, a 64-hex content hash, a span, and four properties in the `{"string": ...}` union form |
| Fixed columns | about 400 | `RepositoryID` 27, `RecordID` 33, `Lineage` 33, `SourceID` and `TargetID` 33 each, `FactDigest` 71 (`sha256:` plus hex), `CommitFrom` and `CommitTo` 40 each, `SearchHash` 64, `Kind`, `Name`, generations |

Edges are 82% of the rows and 74% of the payload bytes. The five biggest
kinds are `calls` (83,494), `references` (71,264), `uses_type` (54,790),
`contains` (51,468) and `implements` (2,332).

## What stays as it is

- **Per-occurrence edges and their anchors.** They are the fidelity. The
  content hash on every anchor is what makes `read_source` a verified cut
  rather than a guess, and looking it up through `CGFileIdentities` on every
  neighbor read would add a round trip.
- **Every index.** `CGEdgesBySource` and `CGEdgesByTarget` serve neighbors,
  callers, callees and impact from the index with a base-row join for the
  payload ([records.go:343](../../../packages/go/code-graph/storage/spanner/records.go#L343)).
  `CGRecordsByLineage` serves the differ and lineage retirement,
  `CGRecordsByKind` serves listing and the count check, `CGRecordsByGenFrom`
  serves the sweep. Index entries are made of key columns, so they shrink
  only when a key or a `STORING` column shrinks.
- **Search tokenization and document text.** `CGSearchDocuments` and its
  index define what a query can match. Changing n-gram sizes or dropping
  identifier forms changes search results and is out of scope.
- **Record ids.** `kind:base64url` ids are the API surface of every node and
  edge and the key of every index. Shortening them is a different project.

## Phase 1: a compact record payload

Replace `json.Marshal(graph.Fact)` in `Payload` with a versioned binary
encoding, decoded at the one place rows become versions
([records.go:88](../../../packages/go/code-graph/storage/spanner/records.go#L88)).

**Format.** The first byte is a tag. JSON always begins with `{` (0x7B), so
legacy rows need no migration to be recognized:

| Tag | Body |
|---|---|
| `{` | legacy JSON, decoded as today |
| `0x01` | protobuf `Fact`, described below |
| `0x02` | protobuf `Fact`, DEFLATE-compressed |
| anything else | `graph.ErrIntegrity` |

**Message.** A new package `packages/go/code-graph/storage/spanner/recordcodec` with a
checked-in `record.proto` and generated Go. `google.golang.org/protobuf` is
already an indirect dependency of the Spanner client; a Makefile target pins
`protoc-gen-go` and CI fails when the generated file is stale.

```proto
syntax = "proto3";
package codegraph.record.v1;

message Fact { oneof fact { Node node = 1; Edge edge = 2; } }

message Node {
  Id id = 1; string kind = 2; string name = 3; string qualified_name = 4;
  Anchor source = 5; repeated Property properties = 6;
}
message Edge {
  Id id = 1; string kind = 2; Id source_id = 3; Id target_id = 4;
  Anchor source = 5; repeated Property properties = 6;
}
// kind:token. token is the 16 raw bytes when the text is canonical
// base64url of 16 bytes; otherwise raw carries the text unchanged.
message Id { string kind = 1; bytes token = 2; string raw = 3; }
message Anchor {
  Id lineage = 1; bytes content_sha256 = 2; string content_raw = 3;
  Position start = 4; Position end = 5;
}
message Position { uint64 byte_offset = 1; uint32 line = 2; uint32 column = 3; }
message Property {
  oneof key { uint32 known_key = 1; string custom_key = 2; }
  oneof value {
    string text = 3; sint64 int64 = 4; double float64 = 5; bool bool = 6;
    StringList strings = 7;
    Id id_text = 8;      // a string value that is a canonical id
    bytes hex32_text = 9; // a string value that is 64 lowercase hex chars
    bytes sha256_text = 10; // a string value that is "sha256:" + 64 hex
  }
}
message StringList { repeated string values = 1; }
```

Rules that keep it lossless:

- Every specialised form (`token`, `content_sha256`, `id_text`, `hex32_text`,
  `sha256_text`) is used only when re-encoding the bytes reproduces the
  original string byte for byte. Otherwise the raw string is stored. The
  encoder checks this on every value; there is no path that guesses.
- `known_key` is an append-only registry of property names the projector
  emits today (`status`, `provenance`, `occurrence_id`, `occurrence_kind`,
  `file_path`, `language`, `signature`, `canonical_signature`,
  `source_text`, `docstring`, `owner_key`, `search_document_hash`,
  `search_document_version`, `type_kind`, `source_set_id`, `module_id`,
  `definition_digest`). A test asserts the registry against the strings in
  the projector. Unknown keys use `custom_key`.
- Property order is not significant in `graph.Node.Properties` (a map), so
  the encoder sorts by key for determinism; the decoder rebuilds the map.
- `PropertyValue` is a closed union with exactly one field set, which the
  `oneof` mirrors. `Int64` never passes through a float.
- The absent anchor (`Source == nil`) and the absent span position are
  encoded as absent messages, never as zero values.

**Compression.** After encoding, if the body is above 1 KiB, compress it
with `compress/flate` and keep the result only if it is smaller, using tag
`0x02`. Edges are about 190 bytes and never qualify; method nodes carrying
`source_text` and `docstring` do, at roughly three to one.

**Size.** A `calls` edge goes from 578 bytes to about 190: three ids at 20
bytes, a kind, an anchor at 68 bytes (16-byte lineage, 32-byte hash, two
positions), and four properties at about 56 bytes. Nodes average about 350
bytes after compression. Payload for adk-java drops from 184 MB to about 66
MB.

**Hooks.**

- `newRecordRow` ([loader.go:83](../../../packages/go/code-graph/storage/spanner/loader.go#L83))
  calls `recordcodec.Encode` instead of `payload(f)`. `MaxRecordBytes`
  applies to the encoded bytes; the decoded side is already bounded by
  `MaxTextBytes` and `MaxPropertyCount` in `graph.Validate`.
- `versionFromRow` calls `recordcodec.Decode`. Every reader in `records.go`
  and the sweep's document restore go through it. Two readers unmarshal the
  node payload themselves and switch to the codec too: the search-document
  backfill ([documents.go:99](../../../packages/go/code-graph/storage/spanner/documents.go#L99))
  and the vector-branch candidate check
  ([search.go:149](../../../packages/go/code-graph/storage/spanner/search.go#L149)). The first
  task below adds a test that fails on any further direct decode.
- `CGFileIdentities` gets the same tag scheme
  ([identities.go:75](../../../packages/go/code-graph/storage/spanner/identities.go#L75)): the
  JSON map DEFLATE-compressed under `0x02`, decoded by tag. About 18 KiB
  becomes about 4 KiB per file, and the matcher reads one row per affected
  file on every incremental run.
- A `CODEGRAPH_RECORD_ENCODING` setting in `serviceconfig` with values
  `json` (default at first) and `compact` selects what the loader writes.
  Decoding always accepts both.

**Deploy order.** Every process that opens the database (worker, admin, api,
mcp) must decode the new tags before any worker writes them. Ship the
release with `json` as the default, deploy all four, then set `compact` on
the workers. A process on the old release that meets a compact row fails
with `ErrIntegrity` rather than misreading it.

## Phase 2: stop storing the digest

`FactDigest` costs 71 bytes per row and another 71 per `CGRecordsByLineage`
entry, about 46 MB on adk-java, and no reader needs it stored:

- The differ already falls back to `v.Fact.Digest()` when the column is
  empty ([differ.go:216](../internal/graphanalysis/differ.go#L216)).
- No API or MCP response carries it (checked: no reference to `FactDigest`
  in `apps/api`, `apps/forge-codegraph-mcp`, `apps/forge-web` or `packages/go/code-graph/agentquery`).
- `RecordsByLineage` selects the base row, not the stored column.

Steps, each safe on its own:

1. `versionFromRow` computes the digest from the decoded fact when the
   column is empty, so `Version.FactDigest` stays populated for every caller.
2. DDL: `ALTER TABLE CGRecords ALTER COLUMN FactDigest STRING(71)` (drop
   `NOT NULL`); the loader writes `NULL`.
3. Rebuild the lineage index without the stored column: create
   `CGRecordsByLineage2 ON CGRecords(RepositoryID, Lineage, GenFrom) STORING (GenTo)`,
   switch the query hint in `RecordsByLineage`, drop the old index.
4. Later, `DROP COLUMN FactDigest` once no release reads it.

Recomputing a digest is one JSON marshal per decoded row, microseconds
against a Spanner round trip, and it happens only where the differ or a
history caller reads the value.

## Phase 3: derive commits from generations

`CommitFrom` and `CommitTo` repeat, on every row, the commit of the
generation that opened and closed it. `CGRuns` already maps generation to
commit and `GenerationCommit` reads it
([records.go:171](../../../packages/go/code-graph/storage/spanner/records.go#L171)). Dropping
the two columns saves 40 to 80 bytes and two cells per row, about 20 MB on
adk-java.

This touches more code than phase 2: `versionFromRow` fills the commits
from a per-repository generation map cached for the transaction,
`CloseEdgesTouching` and the sweep stop writing `CommitTo`, and
`Version.Validate` keeps requiring 40-character commits so the API shape is
unchanged. It is optional; do it only if phases 1 and 2 leave load time
where the bytes still matter.

## Phase 4: one copy of the document text

`CGSearchEmbeddings.DocumentText` is written
([search.go:206](../../../packages/go/code-graph/storage/spanner/search.go#L206)) and never
read: the write-time freshness check recomputes the text from the node, and
search hits read `CGSearchDocuments`. Write the empty string and reclaim
about 9 MiB on adk-java with no DDL; rows refresh on the next re-embed. The
column can be dropped in a later release.

## Phase 5, opt-in: shorter embeddings

Vectors are 110 MiB, 13% of the database. `text-embedding-3-large` supports
requesting 1,024 dimensions, which cuts that to a third. It is a new
database (the vector length is fixed at provisioning, see
[spanner-vector-index](../../../packages/go/code-graph/storage/spanner/schema.go)) and it
changes semantic-search ranking, so it stays behind a retrieval evaluation:
the canned question set from the search report must rank the same top hit
for at least 95% of questions before the dimension is reduced. Not part of
the compaction release.

## Backfilling existing repositories

A `codegraph-admin recode-records -repository-id <id>` command rewrites
`Payload` in place for rows in the legacy encoding:

- Scans `CGRecords` by primary key range in pages of 1,000, decodes each
  row, re-encodes, and issues `Update` mutations that touch the `Payload`
  cell only. Keys, generations, digests and every other cell are untouched,
  so the version history is byte-for-byte the same to every reader.
- Runs `-verify` first: decode legacy, encode, decode again, compare the
  facts for equality, and compare `Fact.Digest()` with the stored
  `FactDigest`. The rewrite refuses to start while any row fails.
- Refuses to run while the repository has a `RUNNING` job, and is resumable
  from the last key it committed. A loader never rewrites the `Payload` of
  an existing version, so a concurrent load could not conflict even without
  that guard.
- Reports rows rewritten and bytes before and after.

The same command handles `CGFileIdentities` and the phase 4 embedding rows.

## Verification gates

Each phase ships only when all three pass.

**Fidelity.**

- Property-based round-trip test over random facts: every property type,
  ids that are and are not canonical base64url, hashes that are and are not
  hex, empty and absent anchors, `int64` extremes, multi-byte text, and
  64 KiB properties. Encode, decode, `reflect.DeepEqual`, and equal
  `Fact.Digest()`.
- Fixture rows: the adk-java `calls` edge and `method` node from this
  document, with asserted encoded sizes so a regression in the encoder is a
  test failure, not a surprise in the next size report.
- Emulator integration test extending `TestPipelineGenerations`: after the
  load, read every open record through `GetNodes`, `Neighbors` in both
  directions, `History`, `RecordsByLineage` and `ListNodes`/`ListEdges` by
  kind, and compare each decoded fact to the fact the projector emitted.
  Today the pipeline verifies counts; this verifies content.
- On the managed adk-java database, `recode-records -verify` over all
  325,650 rows before the first rewrite.

**Performance.** A benchmark command against a fixed database, run before
and after on the same instance:

- `neighbors` at depth 1 and 2 from the 20 best-connected nodes,
  `callers` and `impact` at depth 3 from the 20 most-overridden methods,
  `explore` over the ten questions in the excalidraw report, `history` and
  `find_symbol` for 50 ids and names.
- Records p50 and p95 latency and Spanner bytes read per call (from the
  query statistics tables). Acceptance: p95 within noise of the baseline,
  bytes read lower.
- Replay the adk-java ingestion into an empty database: the `load` stage
  duration (357 s at generation 1) and mutation bytes must not rise; a
  drop is expected because commit cost scales with bytes.

**Storage.** `TABLE_SIZES_STATS_1HOUR` per table and the Monitoring
`instance/storage/used_bytes` series, on adk-java, after the backfill has
settled for two hours.

## Expected result on adk-java

| After | `CGRecords` | Indexes | Other | Tables total | Change |
|---|---:|---:|---:|---:|---:|
| Today | 345 | 250 | 220 | 815 | |
| Phase 1 | about 200 | 250 | 205 | about 655 | -20% |
| Phase 2 | about 180 | 227 | 205 | about 610 | -25% |
| Phase 3 | about 160 | 227 | 205 | about 590 | -28% |
| Phase 4 | 160 | 227 | 196 | about 580 | -29% |
| Phase 5 (opt-in) | 160 | 227 | 123 | about 510 | -37% |

The loader ships about 60% fewer bytes per record after phase 1, which is
where the initial load of a large repository gains.

## Work breakdown

1. **Decode inventory test.** Route the backfill and vector-candidate
   readers through `versionFromRow` or the codec, then add a test in
   `packages/go/code-graph/storage/spanner` that fails if any file other than
   `records.go`, `identities.go` and the codec package unmarshals a
   `Payload` column. Establishes the decode points before the format changes.
2. **`recordcodec` package.** `record.proto`, generated code, `Encode`,
   `Decode`, the known-key registry, the lossless specialisations, DEFLATE
   above 1 KiB, and the round-trip property test.
3. **Store integration.** `newRecordRow`, `versionFromRow`,
   `PutFileIdentities`, `GetFileIdentities`, the `CODEGRAPH_RECORD_ENCODING`
   setting, `MaxRecordBytes` applied to encoded bytes, and the extended
   emulator pipeline test.
4. **Benchmark command** under `apps/api/cmd/codegraph-search` alongside
   the existing reindex flag, plus the baseline numbers for adk-java
   recorded in `reports/`.
5. **Backfill command** in `codegraph-admin` with `-verify`.
6. **Release 1**: decode both formats, default `json`. Deploy all
   processes. Switch workers to `compact`. Backfill adk-java and compare
   against the baseline.
7. **Phase 2 DDL** as a numbered file under `schema/migrations/` applied
   with `gcloud spanner databases ddl update`, the base DDL updated for new
   databases, and `Store.Ping` checking `FactDigest` nullability the way it
   checks the vector length today, so a process fails at startup against an
   unmigrated database.
8. **Phase 4** write change, then phases 3 and 5 by separate decision.

## Rejected

- Collapsing repeated call sites into one edge, or removing the content
  hash from anchors: both lose evidence that impact and `read_source`
  depend on.
- Dropping or narrowing any secondary index: every one has a reader on a
  latency-sensitive path or on the sweep.
- Changing search tokenization to shrink the substring token list: it
  changes which queries match.
- Re-ingesting with an analysis refresh instead of a backfill: an update
  closes every old version and inserts a new one, doubling rows before
  garbage collection.
- Compressing the legacy JSON in place: about 1.7 to one on an edge, since
  ids and hashes do not compress; the binary form is 3 to one before any
  compression.
