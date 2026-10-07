# Closing the breadth gap

The comparison with the codegraph-streaming POC (in the ei-aitiger-codegraph repository)
ended with one sentence: the new implementation knows more about the Java it
sees and is right about it; the POC sees more of the repository and is often
wrong about the relationships. This document records the code that has to be
added so that this implementation also sees the rest of the repository,
without giving up the property that made it worth building.

## Rules for every item below

1. **Provenance is a fact, not a score.** Every edge and node gets a
   `provenance` property with one of `compiler`, `syntax`, `framework_rule`,
   `extracted`. Consumers can filter on it; nothing labelled `syntax` or
   `framework_rule` is ever presented as a compiler binding. The POC's
   0.55 to 0.98 confidence numbers were its failure mode and are not coming
   back.
2. **Nothing new in the worker loop.** Languages and extractors arrive as
   `languages.Adapter` entries; framework facts are emitted by a language-owned
   enrichment stage; storage stays `CGRecords` versions. The registry was built
   for this; the pipeline stages are language-neutral.
3. **Every item ships with a query-side check.** The point is agent
   usefulness, so acceptance is an API call against a fixture, not a count.

## Gaps and the code that closes them

### G1. Find a symbol by name (done 2026-09-14)

*Missing:* no endpoint resolves a name or qualified name to a node. Finding
`LlmAgent` in the ADK graph meant paging 1,001 class nodes.

*Code:* a `CGRecordsByName` index on `(RepositoryID, RecordKind, Name, GenFrom)`
storing `GenTo` and `Kind`; a `QualifiedName` column (today it lives only in
the payload) with its own index; a `Store.FindNodes(repo, name|qualifiedName,
kinds, generation)` reader; and `GET /v1/repos/{repo}/symbols?name=&qualified_name=&kind=`.
Extend `codegraph-search` with `-name`.

*Accept:* `LlmAgent` and `com.google.adk.agents.LlmAgent` each return the
class node in one request at any generation.

### G2. Lexical search that works without embeddings (done 2026-09-14)

*Missing:* `/v1/search` returns 400 unless an embedding provider is
configured, because the lexical branch is a substring scan joined to the
embeddings table.

*Code:* a `CGSearchDocuments` table written by the loader for every node that
has a search document (`codesearch.FromNode`), with `TOKENLIST` columns from
`TOKENIZE_FULLTEXT` over the document text and `TOKENIZE_SUBSTRING` over
identifiers split on camelCase, plus a `SEARCH INDEX`. The lexical branch
becomes `SEARCH` plus `SCORE`; reciprocal rank fusion with the vector branch
stays as it is. Spanner's `SCORE` is term-frequency based, not BM25; that is
acceptable because fusion, kind and path filters do the ranking work. The
emulator pinned in `compose.yaml` executes `SEARCH`, `SCORE`, `SNIPPET` and
`SEARCH_SUBSTRING`, so this is testable locally. Remove the exact-scan TODO
in `packages/go/code-graph/storage/spanner/search.go`.

*Accept:* with no provider configured, `q=runAsyncImpl` and `q=async impl`
both rank the method first; `/v1/search` never returns 400 for a missing
provider.

### G3. Override-aware impact and callers

*Missing:* `/impact` walks incoming edges only. The impact of an overriding
method is empty, because callers call the base declaration. It also issues
one `Neighbors` query per frontier node behind a hard 60 s write timeout, so
a depth-2 walk from a method with 30 overriders times out on the emulator.

*Code:* before walking, expand the root through outgoing `overrides` and
`implements` to every declaration it can be dispatched from, and expand each
visited static target through incoming `overrides` and `implements` to its
overriders; walk incoming `calls`, `references`, `uses_type` and `inherits`
from that set. Batch the frontier into one `CGEdgesByTarget` query per depth
with `TargetID IN UNNEST(...)`. Return partial results with `truncated: true`
under a request budget instead of closing the connection. Expose the same
expansion as `GET /v1/repos/{repo}/nodes/{id}/callers`.

*Accept:* impact of `LlmAgent.runAsyncImpl` includes `BaseAgent.runAsync`
and its callers; depth 3 from `BaseAgent.runAsyncImpl` completes on the
emulator.

### G4. Framework and topology edges

*Missing:* routes, handlers, HTTP client calls, dependency injection. The
`framework_binding` edge kind is reserved and nothing produces it.

*Code:* a Java enrichment stage between resolve and project that reads
resolved annotation types (the resolver already binds annotation type uses, so
`org.springframework.web.bind.annotation.GetMapping` is identified by
canonical key, not simple name) and the typed annotation arguments the IR
keeps (`path` versus `produces` are distinct). It emits:

- `route` nodes (method, path pattern, framework) and `handles` edges from
  route to handler for Spring MVC and WebFlux, JAX-RS and Micronaut;
- `http_calls` edges from a call site to a route when the client is
  `RestTemplate`, `WebClient`, `HttpClient` or a Feign interface and the path
  is a compile-time constant, otherwise to an external endpoint node;
- `injects` edges for Spring constructor, field and `@Bean` injection,
  resolved through the javac type of the injection point;
- all with `provenance: framework_rule` and the rule name.

Cross-repository route linking is a query-time join on route nodes across
repositories, not an ingest-time edge.

*Accept:* a Spring PetClinic fixture produces every controller route with its
handler, client calls land on routes, and `Map.get` produces no `http_calls`.

### G5. Non-Java languages

*Missing:* Go, Python, TypeScript, JavaScript, C#, Groovy and Gradle.

*Code, in dependency order:*

1. Pipeline routing (done 2026-09-14): `ingestion.Config.Resolvers` is a map,
   `Bootstrap` keeps one resolver per language, compilation contexts are
   dispatched to the resolver of their `SourceSet.Language`, and the build
   context composes one provider per language with a syntax fallback for a
   language whose build is absent (`internal/buildcontext/composite`). The
   worker registers and the admin API reports the list of languages.
2. Syntax-tier resolution (done 2026-09-14 for TypeScript/JavaScript):
   `semantic.Lookup.Provenance` distinguishes `compiler` from `syntax`
   bindings and the projector copies it onto edges.
   `internal/resolve/typescript` accepts the syntax fallback context, labels
   every lookup it proves from scopes, imports and declared types as
   `provenance: syntax` and never fabricates a target; a name it cannot bind
   is an unresolved lookup with cause `analysis_limitation` or
   `external_dependency`.
3. Parser adapters (done 2026-09-14 for TypeScript/JavaScript):
   `internal/parser/typescript` registers the upstream `typescript`, `tsx`
   and `javascript` grammars under one language id and emits
   `ir.SourceFile`. `pkg/ir` gained the constructs that had no home:
   `type_alias`, `namespace` and `variable` declarations, `Import.Module`,
   the `alias` and `component` type-use roles, `object_literal` and `markup`
   expressions, and `function`, `structural`, `literal` and `operator` types.
   The same pattern applies to the other languages.
4. Build providers per language for the `Discover` slot (done 2026-09-14
   for TypeScript/JavaScript): `internal/languages/typescript/project` reads
   `tsconfig`/`jsconfig` path mappings and `package.json` or pnpm workspaces
   into the source set's language options, and the resolver follows
   re-exports, aliases and workspace package names. `pyproject` and
   `requirements`, `go.mod` remain for their languages. The POC's
   `tsresolve` and `pyresolve` are the reference implementations.
5. Compiler-backed resolvers where one exists, as the second step for each
   language: `tsc` language service for TypeScript, `gopls` or `go/types` for
   Go, Roslyn for C#. Syntax-tier resolvers remain the fallback for repositories
   that do not build.

Order the languages by the POC's own resolver quality: TypeScript and
JavaScript first, then Python, Go, C#, Groovy.

*Accept:* the POC's pinned fixtures (Click, chi, express, hono) ingest; each
language gets a resolution report with the same shape as the ADK report.

### G6. Dependency and structure nodes

*Missing:* project, folder, package and module nodes; package dependencies.
Module and source set are string properties on `source_file` nodes.

*Code:* the projector emits, per generation, a `repository` node, `module`
nodes from the build inventory with coordinates, `package` nodes from
declared package clauses, and `folder` nodes from paths, all joined by
`contains`. It also emits `package_dependency` nodes for every artifact in
the build context (the Maven provider already records coordinates and
fingerprints for 671 artifacts on ADK) and links each `external_symbol` to
its artifact with `provided_by`. This is the cheapest high-value item: the
data is already in the sealed context.

*Accept:* an outline query for `core/src/main/java/com/google/adk/agents`
returns its files and types; `guava` appears once per version with the
external symbols it provides.

### G7. Non-code artifacts

*Missing:* SQL, Terraform, Markdown, YAML and JSON configuration, `.env`,
Spring XML, HTML, `package.json`, `go.mod`, `pyproject`.

*Code:* extractor adapters registered by extension that emit `ir.SourceFile`
records with declaration kinds `sql_table`, `sql_view`, `terraform_resource`,
`config_key`, `env_var`, `markdown_section`, `bean` and occurrence tables for
their references; a trivial resolver that binds by exact name with
`provenance: extracted`; projector edge kinds `reads_from`, `writes`,
`requires_env`, `configures`, `includes`. Environment-variable usage in Java
(`System.getenv`) is a framework rule under G4 that targets `env_var` nodes.

*Accept:* a Spring XML bean resolves to its class node; a `System.getenv("X")`
call links to the `env_var` declared in `.env.example`.

### G8. Lombok and AST-modifying annotation processors

*Done (2026-09-26, resolver `java-javac-binding-v8`):* a source set with
`org.projectlombok:lombok` on its classpath is attributed with
`-processorpath` limited to that JAR, then the service's `lombok_jar` when it
cannot run on the service JDK, then none (a warning on the run). Code Lombok
generates is not reported; its members and types are `lombok_generated`
derived symbols of their nearest source type, which is their contributor,
reached from source attribution and from reactor bytecode alike. Details
below are the original plan.

*Missing:* the javac bridge runs with `-proc:none`, so calls to
Lombok-generated members become unresolved lookups.

*Code:* when the Maven classpath contains a known AST-modifying processor,
run the bridge with `-processorpath` limited to that JAR and annotation
processing enabled; members with no source span come back from javac as
synthetic elements, which the resolver already knows how to record as
derived symbols (`rule: lombok_generated`, contributor: the annotated field
or class). Keep `-proc:none` for everything else.

*Accept:* a Lombok fixture resolves `getFoo()` and `builder()` calls to
derived symbols contained by the annotated class.

### G9. HTTP ingestion, OpenAPI and an MCP server

*Missing:* work is admitted only through `POST /v1/admin/runs` on
`codegraph-admin` with an explicit deployment ID and sequence; there is no
OpenAPI document.

*Code:* `POST /v1/ingestions` on `codegraph-admin` wrapping `Store.Admit`, with
the sequence allocated per repository when the caller has none; `GET
/v1/ingestions` over `ListRuns`; an OpenAPI 3.1 document generated from the
handler types; and an MCP server exposing `search`, `find_symbol`, `node`,
`neighbors`, `callers`, `impact`, `history` and `source` as tools with the
same JSON shapes.

*Accept:* an agent can ingest a repository and answer "who calls X" using
only the MCP tools.

### G10. Local embedding provider

*Missing:* semantic search needs an OpenAI-compatible key, so a
self-contained stack has no vector branch.

*Code:* a deterministic hash provider behind `codesearch.Provider` for
development and tests, selected by `CODEGRAPH_EMBEDDING_PROVIDER=hash`.
Lower priority once G2 lands.

### G11. Scale evidence

*Missing:* the new pipeline has been measured on 597 files against the
emulator. The POC has a 39,280-file, 1.9-million-node baseline.

*Code:* none until measured. Run the Elasticsearch commit the POC pinned
against managed Spanner, record stage durations, loader throughput and
resolver heap, and fix what the numbers show. G3's batching is the first
known scaling defect.

## Sequencing

| Order | Item | Size | Depends on |
|---|---|---|---|
| 1 | G1 name lookup | S | done: `CGSearchDocumentsByName`, `Store.FindNodes`, `/v1/repos/{repo}/symbols` |
| 2 | G2 lexical search | M | done: `CGSearchDocuments` search index, weighted fusion, exact tier, graph rerank |
| 3 | G3 override-aware impact | M | none |
| 4 | G6 dependency and structure nodes | S | none |
| 5 | G4 framework edges (Spring first) | L | G6 for `env_var` targets |
| 6 | G9 HTTP ingestion, OpenAPI, MCP | M | G1, G3 |
| 7 | G8 Lombok | M | none |
| 8 | G5 languages: TypeScript and JavaScript (parser, syntax-tier resolver and `tsconfig`/workspace discovery done 2026-09-14 and measured on excalidraw, see `reports/typescript-excalidraw-2026-09-14`; the compiler tier with installed npm packages done 2026-09-27, see [typescript-compiler.md](typescript-compiler.md)) | L | routing change (5.1, 5.2) |
| 9 | G7 non-code extractors | M | G5.1, G5.2 |
| 10 | G5 languages: Python, Go, C#, Groovy | L each | G5.3 |
| 11 | G10 hash provider | S | none |
| 12 | G11 scale run | M | G3 |

Items 1 to 4 are two weeks of work and remove every defect the comparison
found in the current API. Item 5 is where the POC's breadth starts to come
back. Items 8 to 10 are the long tail and should each ship with a report in
`reports/` like the ADK one.

## Non-goals

- Reintroducing heuristic call resolution as `resolved`. A guess is a
  `syntax`-provenance lookup or an unresolved lookup, never a compiler fact.
- Near-clone `similar_to` edges and per-symbol chunk nodes. Source text is
  already on the node; clones can be a search-time feature.
- Full replacement ingestion. Generations, identity continuity and history
  are the reason this implementation exists.
