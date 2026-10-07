# Architecture

## Principle

Spanner is the system of record for what agents query: repositories and their
live generation, runs and jobs, the versioned graph, per-file identity maps,
retained source and embeddings. Everything else is derived from (repository,
commit, configuration) and disposable: git mirrors, the syntax cache, the
run-local index. Because loading a generation is idempotent and invisible
until one pointer flips, there is no write-ahead plan, no readback
verification and no validate-on-read. Verification happens at three
boundaries: parser output, javac output, and the count check before the flip.

## Generations

`CGRecords` rows are versions keyed by (repository, kind, id, gen_from). A
reader at live generation L sees a version iff `gen_from <= L AND (gen_to IS
NULL OR gen_to > L)`. The loader for generation G inserts new versions with
`gen_from = G`, closes replaced versions with `gen_to = G`, and closes retired
versions with `gen_to = G, retired = true`; all carry the merge commit. Until
`CGRepositories.LiveGeneration` becomes G nothing is visible. An abandoned
generation is swept (rows above live deleted, closures above live reopened)
before the next attempt loads.

Every durable write of a run is fenced. The loader, the sweep, the edge
cascade and the identity-map writer commit each batch in one read-write
transaction that first reads the repository row's fence cells, verifies the
lease's owner, run and token, and checks the live generation (still below
the loading generation, or still the generation the sweep and cascade were
planned against). Every acquisition, a takeover of an expired lease included,
rewrites those cells with a higher token, so a worker whose lease was taken
over cannot land anything, not even a batch that was already in flight.
Expiry is enforced by the renewer and by the lease snapshot taken between
batches rather than inside each commit, and a renewal writes only the expiry
cell: Spanner locks per cell, so renewals and batch commits never wait on
each other. Publish verifies the lease strictly, expiry against database time
included, then additionally reads the live run and refuses a deployment
whose sequence is below the live one, or equal without being an analysis
refresh, and supersedes every older ACCEPTED, RUNNING or FAILED run in the
same transaction. A run that starts behind the live deployment ends
SUPERSEDED before doing any work; its job completes without retries.

## Admission

A deployment is admitted through `codegraph-admin`, whose admission route
creates the run and its job in one transaction, bound to an analysis
configuration digest. The digest identifies what the
worker computes: the parser registry, the resolver version and policy, and
the parser, build and discovery limits. A worker claims only jobs that carry
its own digest. Admission runs in processes that never link the analysis
stack, so each worker publishes the digest itself: one `CGWorkers` row per
process with queue, owner, digest, languages, build mode and retry limit,
written at startup, refreshed on every health tick and deleted on a clean
shutdown. Admission reads the workers whose heartbeat is within the admin's
window, requires them to agree on one digest, and otherwise refuses (no
worker, or a divided fleet) rather than enqueue a job nothing could claim.
The overview counts READY jobs whose digest differs from the fleet's as
mismatched.

## Incremental scope

Git's changed paths are expanded to every file variant of those paths, every
file with an open edge into an entity of a changed or deleted file (from the
graph's incoming-edge index), and every compilation context that contains one
of those files or lists such a context's output on its classpath. All files of
an affected context are re-attributed and re-projected; the diff keeps
unchanged files write-free. Files outside affected contexts are untouched.

Before git is consulted, the run fingerprints every compilation context's
build inputs (the JDK, compiler settings, the ordered class and module paths
with each artifact's content digest, generated roots and selection patterns;
never the source roots, which per-file hashes cover) and compares them with
the inputs recorded for the live generation in `CGGenerationInputs`. A
context whose digest differs is invalidated and expanded downstream exactly
like a changed file, so a POM-only commit re-attributes what it affects and
nothing else. An analysis refresh, a baseline analysed under another
configuration digest, or a baseline whose inputs were never recorded
recompute every file while still honouring git's deletions and renames.
Every published generation records its own inputs before the flip.

## Identities

The matcher reads one `CGFileIdentities` row per affected file (the previous
generation's map: declaration IDs with spans, names, owners and resolved keys;
occurrence IDs with enclosing entities) and continues entity and occurrence
IDs when the file's content is unchanged or when a unique canonical key
persists in the same lineage. A git-detected rename continues the lineage.
Cross-file targets in unchanged files resolve through those maps' spans, so
their IR is never loaded.

A renamed file is projected under its new lineage with the continued IDs;
each continued record becomes an update that closes the old-lineage version
and opens the new one. Retiring the old lineage skips every key projected in
the run, and the edge cascade skips every node that is open at the loading
generation, so edges from unchanged files into a renamed file's entities
survive and the history shows a replacement, never a retirement.

## Edges

`contains`, `calls`, `uses_type`, `references`, `inherits`,
`framework_binding`, `derived_from`, `type_component`, `has_chunk`, plus
`overrides` (method to the methods it overrides, via javac's `Elements.overrides`)
and `implements` (a lambda or method reference to the single abstract method of
its functional interface). Impact queries expand a static call target through
`overrides` and `implements` to its runtime candidates.

## Search

Method, constructor, field and initializer nodes carry their source text;
type nodes carry signature and documentation. Every such node has a retrieval
document (`codesearch.FromNode`) whose hash is stored on its record. The
loader writes the document and the node's identifier forms (the name, the
qualified name, and their dotted, camelCase and snake_case fragments) to
`CGSearchDocuments`, indexed by `CGSearchDocumentsIndex`: full-text tokens of
the document, full-text and substring tokens of the identifiers, partitioned
by repository. A document is re-embedded only when its hash changes.
The embedding pass runs after Publish, when lexical search covers the live
generation. With an embedding provider configured, ingestion remains RUNNING
until the required index is COMPLETE. Any embedding failure makes the run
FAILED with an INCOMPLETE index and propagates to job settlement. A transient
retry resumes missing vectors on that same generation without parsing or
loading the graph again. Progress and completion require the current repository
lease and live generation, so an expired worker cannot overwrite a successor.
The index records its model, dimensions, counts, error, and `embed` duration.
Workers configured without a provider retain lexical-only ingestion. Windows are
packed sixteen to a request across documents, a configurable number of
requests are in flight, and each page's vectors are committed while the next
page is embedded. A type's document also lists the members it declares
("Members:" lines of kind and name, bounded), so a question about a concept
finds the class that groups its methods, not only the methods.

Search reads the query first (`codesearch.ParseQuery`). An identifier or a
partial identifier is used as written. A sentence of three or more words is
a question: its function words and generic code words ("who calls the
method that") are dropped from the lexical terms, identifier-shaped words
(camelCase, dotted, snake_case, or followed by parentheses) are taken as
symbols, and words with a path separator or a source extension as paths.
What was understood is returned with the hits.

Search is then two-level. Level one runs up to two branches per repository
inside one read-only transaction: the lexical branch queries the search
index with the content terms joined by `OR` and ranks by `SCORE` (query
enhancement on managed Spanner adds spelling, synonyms, plurals and IDF
weighting), and the vector branch embeds the question and ranks stored
embeddings by cosine distance. Each branch yields ranks that weighted
reciprocal rank fusion combines, identifier-shaped queries favouring the
lexical branch and sentences the vector branch, so either branch works
alone. Level two reranks the fused candidates: a node whose name or
qualified name is the query, or a symbol the question names (its simple
name and, for `Owner.member`, its owner), is an exact match and goes to the
top; a type or callable whose name is merely a plain word of the question is
a name match and edges ahead; better-connected declarations edge ahead of
isolated ones; hits near the caller's current file, or under a path the
question mentions, edge ahead of far ones. Only nodes open at the live
generation whose document or embedding still matches their current text are
candidates, so a generation still loading never shows through, and the
sweep restores documents an abandoned generation rewrote. Hits carry the
signature, a snippet around the first matched term, the matched terms, the
exact-match and name-match flags and caller and callee counts, so an agent
can pick which hits to expand. `/v1/repos/{repo}/symbols` resolves a name or
qualified name directly through the same index.

The vector branch always runs on a Spanner vector index. `CGSearchEmbeddings`
stores each document's embedding in an `ARRAY<FLOAT32>` column whose
`vector_length` is fixed when the database is created, from
`CODEGRAPH_EMBEDDING_DIMENSIONS`, and `CGSearchEmbeddingsVector` is a
cosine vector index over it storing the document hash and version. The
query is `APPROX_COSINE_DISTANCE` with `FORCE_INDEX`, ordered by that
distance alone under a bounded `LIMIT`, filtered by repository, model and
document version; the open-at-live check, the document-hash check and the
kind and path filters are applied to the returned candidates, and the
candidate pool and leaves searched are widened when such filters are
present. Every process opens the store with the configured dimension and
`Ping` verifies it against the column's declared length, so a mismatched
provider fails at startup instead of at the first write. The emulator
supports the same DDL and query.

## Exploration

`packages/go/code-graph/agentquery` composes the store's reads into the operations an
agent uses to explore, and both `codegraph-api` (HTTP, in `apps/api`) and
`codegraph-mcp` (Model Context Protocol tools, in `apps/forge-codegraph-mcp`) build on it, so
an endpoint and a tool answer identically; ingestion administration is
`codegraph-admin` in `apps/admin` and the worker is `apps/forge-codegraph-worker`. The Spanner store itself lives in
`packages/go/code-graph/storage` and the shared process configuration in
`packages/go/code-graph/serviceconfig`, so the worker, the admin API, the query API and the
MCP server open the same database the same way. `Explore` turns a question into a context pack: the search
hits become seeds with file, lines, byte span, content hash, signature,
snippet and a bounded docstring; the strongest seeds (exact matches first)
are expanded one hop in both directions over the semantic edge kinds
(`calls`, `references`, `uses_type`, `inherits`, `overrides`, `implements`,
`framework_binding`), never over containment; edges between two seeds are
reported as links; and the files involved are counted. Nothing in the pack
is inferred: every related node arrives through a stored edge, and every
span lets the agent read the exact source next. `Impact` walks incoming
dependency edges from the node and from the declarations it overrides or
implements, so callers of a base method are impacted by a change to its
override. `NodeSource` cuts a node's span from the retained file, verified
against its content hash, with optional whole lines of context.

`codegraph-mcp` serves these as read-only tools over streamable HTTP
(`/mcp`, stateless, JSON responses) or stdio: `explore_code`, `search_code`,
`find_symbol`, `get_node`, `neighbors`, `callers`, `callees`, `impact`,
`history`, `read_source`, `read_file` and `repository_state`. The server's
instructions tell a model to start with `explore_code`, read source by node
id, walk with `callers` and `callees`, and run `impact` before changing a
declaration. Repository ids are explicit on every call; the server holds no
per-session state.
