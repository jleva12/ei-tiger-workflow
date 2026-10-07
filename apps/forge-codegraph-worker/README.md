# Codegraph ingestion worker

`codegraph-worker` is Forge's code ingestion engine: it turns a GitHub
repository into a searchable graph of its code (declarations, references,
calls, types, files) with each declaration's source and, optionally, its
embedding, so that codebases can be searched and explored like a knowledge
base. It claims ingestion jobs from the Forge admin API's MySQL, where
organizations queue them for their repositories, and runs one generation per
job into Spanner: fetch, build context, parse, javac attribution, identity
matching, projection, diff, load, flip and embed. This module owns the whole
analysis stack; nothing else in the repository links it. Module path
`ei-aitiger-codegraph/worker`, copied from `forge-aidlc-parent`'s
`apps/forge-codegraph-worker` with the shared modules it links under
[packages/go/code-graph](../../packages/go/code-graph/README.md). It differs
from that copy in where its jobs come from ([How ingestions reach the
worker](#how-ingestions-reach-the-worker)), in how it reads GitHub
([GitHub access](#github-access)) and in its host ports, which sit beside
that repository's.

| Path | Owns |
|---|---|
| `cmd/codegraph-worker` | The worker process: configuration from the environment only, a supervisor that drains on the first signal and exits on the second |
| `internal/workerapp` | Configuration on top of `packages/go/code-graph/serviceconfig`, language bootstrap, the job loop (claim, admit, run, settle: `ingest.go`), health probes, the worker API, worker registration |
| `internal/jobqueue` | The queue of ingestion jobs in the admin API's MySQL (`code_ingestion_jobs`): claims under `FOR UPDATE SKIP LOCKED`, leases, fenced writes |
| `internal/ingestion` | The pipeline and the analysis configuration digest |
| `internal/languages`, `internal/parser/{java,typescript}`, `internal/resolve/{java,typescript}` | Language adapters, the Tree-sitter Java and TypeScript/JavaScript parsers, the javac binding bridge and the TypeScript syntax-tier resolver |
| `internal/buildcontext`, `internal/discovery` | Maven, Gradle, manifest and syntax build providers, composed per language; file discovery |
| `internal/graphanalysis`, `internal/localindex` | Identity matching, projection and diffing; the SQLite run index and syntax cache |
| `internal/repository/github` | Git mirrors and diffs |
| `python-analyzer` | The Pyright bridge the Python resolver runs under Node; `make worker-install` builds `dist/bridge.cjs`, the image ships it at `/opt/codegraph/python` |
| `typescript-analyzer` | The TypeScript compiler bridge the TypeScript resolver runs under Node; `make worker-install` builds `dist/bridge.cjs`, the image ships it at `/opt/codegraph/typescript` |
| `scripts` | The image's entrypoint, and `spanner-cloud.sh`, which creates, updates or resets the managed Cloud Spanner database |
| `docs` | Design notes: architecture, language routing, build context, IR, parser contract, storage compaction, Python ingestion |

## How ingestions reach the worker

The queue of ingestion jobs is the Forge admin API's MySQL: its table
`code_ingestion_jobs`, which the admin API's migrations create
(`0014code_repositories`, `make admin-migrate`). An organization adds a
GitHub repository and asks to ingest it through the admin API
(`/api/v1/organizations/{org}/code-repositories/…`, see its README), which
writes a `QUEUED` row; nothing calls the worker. The worker reaches the
table with `CODEGRAPH_JOBS_MYSQL_DSN` and, in each task slot
(`CODEGRAPH_TASK_CONCURRENCY`):

1. **Claims** the oldest row of its queue (`CODEGRAPH_QUEUE`, `ingestion`)
   whose `eligible_at` has come, with `SELECT … FOR UPDATE SKIP LOCKED`, so
   workers never wait on each other, skipping a repository another worker is
   ingesting. The claim increments the row's `claim_token` and holds a
   45-second lease the worker renews every 10 seconds; while it runs,
   `eligible_at` follows the lease, so the job of a worker that died is
   claimed again when its lease lapses. Every write the worker makes to the
   row is fenced on its token and on the job still running: a worker that
   lost it (the lease lapsed and another claimed it, or the repository was
   removed) changes nothing, and stops.
2. **Admits** the run in Spanner, on its first attempt: it reads the
   branch's head (`git ls-remote`) when no commit was asked for and records
   it on the row first, so a retry ingests the same commit; registers the
   repository in the graph; and admits the run with the submission
   `forge-job-<job id>` and its own analysis configuration digest. A worker
   that died between admitting and recording the run finds it by that
   submission. The run's key goes on the row (`codegraph_repository_id`,
   `run_id`).
3. **Runs** it: fetch, build context, parse, attribution, matching,
   projection, diff, load, publish and embed, under the repository's lease
   in Spanner, so one run per repository at a time.
4. **Settles** the row: `SUCCEEDED` with the generation and the run's counts
   (`metrics`: files, nodes, edges, references resolved, what it left out);
   `SUPERSEDED` when a newer commit was published first (a run superseded
   by one that published the same commit, as another organization's job of
   the repository does, still succeeded); or back to `QUEUED`, or `FAILED`
   (see [Completion and retries](#completion-and-retries)). Failing a job
   also fails its unfinished run, after the row is written.

What a failed row's `error_code` says:

| `error_code` | Meaning |
|---|---|
| `repository_unreadable` | Git could not read the repository: it does not exist, or it is private and `CODEGRAPH_GITHUB_TOKEN` is unset or cannot read it |
| `branch_not_found` | The repository has no such branch |
| `branch_conflict` | The repository's code graph follows another branch (a graph is locked to the first branch it was ingested on) |
| `invalid_repository` | The URL isn't a GitHub repository's |
| `retry_exhausted` | Every attempt failed transiently, or its worker died on the last |
| `job_timeout` | The run went past `CODEGRAPH_TIMEOUT` |
| `permanent_failure`, or the run's own code | A failure another attempt can't get past |

A job carries no configuration digest: whichever worker of the queue claims
it admits the run under its own. At startup and on every health tick the
worker still records itself in Spanner (`CGWorkers`: its queue, owner,
digest, languages, build mode, retry limit and embedding availability) and
removes the row on a clean shutdown; nothing depends on it.

The copy in `forge-aidlc-parent` keeps the queue in Spanner (`CGJobs`) and
admits runs through `POST /v1/runs`; here both are gone. A Spanner database
created before keeps its `CGJobs` table and `CGReadyJobs` index, unused;
drop them by hand if you like (`DROP INDEX CGReadyJobs`, then
`DROP TABLE CGJobs`): the schema tools only ever add.

## Worker API

One trusted caller, such as the Forge admin API, reads the published
graphs, their runs and the ingestion audit over HTTP, and keeps
cross-repository links. The API shares the health listener
(`CODEGRAPH_HEALTH_ADDR`: 8090 natively, 18090 from Compose,
`http://worker:8080` inside the stack) and is on only when
`CODEGRAPH_ADMISSION_TOKEN` is set (named for when it also admitted
ingestions); `make env` generates one (`FORGE_CODEGRAPH_ADMISSION_TOKEN` in
`.env.common`). Every call sends it as `Authorization: Bearer <token>`. The
caller decides who may read which repository: repository IDs are
deterministic (`repo:` and a hash of the URL), so only ever pass on IDs of
repositories the reader's organization has.

| Call | Does |
|---|---|
| `GET /v1/repositories/{repo}/runs/{run}` | The run: `phase`, metrics, failure |
| `GET /v1/repositories/{repo}/runs?limit=&cursor=` | `{"runs", "next_cursor"?}`: the repository's runs, newest deployment first, each the `run` object of the run call; `limit` 1–50, default 10; `next_cursor` is absent on the last page |
| `GET /v1/repositories/{repo}/stats` | Totals of the live graph from one snapshot: `generation`, `branch`, `commit_sha`, `run_id`, `nodes` and `edges` with `node_kinds` and `edge_kinds`, `searchable` nodes, and `embeddings` (`model`, `dimensions`, `current` vectors that match their node, `stored` vectors) for the model this worker embeds with (empty and zero when embeddings are off). Before the first publication, generation 0 and zero counts |
| `GET /v1/repositories/{repo}/graph?kind=&generation=&cursor=` | A sample of published nodes (of one kind, or any), a bounded neighbourhood around each and the edges among them, with the generation, branch and commit read; `generation` 0 or absent is the live one |
| `GET /v1/repositories/{repo}/neighbors?node=&direction=&generation=&limit=&cursor=` | A node's edges (`in`, `out` or `both`, the default) and their far endpoints; `limit` 1–200, default 40 |
| `GET /v1/repositories/{repo}/symbols?name=\|qualified_name=&generation=&limit=` | Nodes with exactly that name or qualified name (one of the two); `limit` default 20 |
| `GET /v1/repositories/{repo}/source?node=&generation=&context=` | A node's retained source, with `context` lines around it (0–200, default 3) |
| `GET /v1/repositories/{repo}/node?node=&generation=` | One node's version |
| `PUT /v1/cross-links/{owner}` | Makes `{"links": [...]}` the owner's (e.g. `team:<id>`) whole set of cross-repository links: each from a node of one repository's graph to a node of another's, with its kind, label and the nodes' qualified names. Stored beside the graphs in `CGCrossLinks`, never in a generation; an empty list removes them. At most 2000 per owner |
| `GET /v1/repositories/{repo}/cross-links?node=&direction=` | `{"hops"}`: the node's cross-repository links (`in`, `out` or `both`, the default), each followed to the node at its far end in that repository's live generation, or `stale` when it's gone |

```sh
token=$(sed -n 's/^FORGE_CODEGRAPH_ADMISSION_TOKEN=//p' .env.common)
curl -s localhost:8090/v1/repositories/<repository id>/stats -H "Authorization: Bearer $token"
```

Failures return `{"code", "message"}`:

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | A missing or out-of-range parameter, or a bad cursor |
| 401 | `unauthenticated` | Missing or wrong token |
| 404 | `not_found` | No such run, repository, node or source |
| 409 | `conflict` | The request conflicts with the graph's state |
| 503 | `unavailable` | Spanner is down; retry |

### GitHub access

Every clone, fetch and `ls-remote` sends `CODEGRAPH_GITHUB_TOKEN` to
github.com, whichever repository it reads and whoever requested the run. The
worker's `.env` takes it from `FORGE_GITHUB_TOKEN` in the repository's
`.env.common`. A fine-grained personal access token with read access to the
contents of the repositories to ingest is enough; a classic token with
`repo` scope works too. Without a token the worker reads GitHub anonymously
and logs so at startup: public repositories ingest, and a job for a
private one fails with `repository_unreadable`. The token travels only as
an HTTP header of Git's own commands (never in a URL or on disk), and the
worker scrubs anything that looks like a GitHub token from the errors it
reports.

The copy in `forge-aidlc-parent` reads GitHub as Forge's GitHub App instead,
with per-repository tokens its admin API mints; this repository has no
GitHub App, so that client (`internal/githubtokens`) was left out.

## Languages

One process serves every language it has an adapter for. In the build modes
each language's own build system describes its source sets and the registry
composes them into one context; a language without a build in the checkout
falls back to a repository-wide syntax profile. The pipeline routes each
compilation context to the resolver of its source set's language and the
analysis digest covers every resolver, so adding an adapter changes the
digest and existing jobs need new admissions. Three adapters are bundled:
Java, bound by javac; TypeScript/JavaScript, bound by the TypeScript
compiler over each project's tsconfig and installed packages
([docs/typescript-compiler.md](docs/typescript-compiler.md)), with a
syntax tier for what it cannot prove; and Python, bound by Pyright
([docs/python-ingestion.md](docs/python-ingestion.md)); see
[docs/language-routing.md](docs/language-routing.md).

The graph holds a repository's own code, not its tests or tool folders.
Discovery leaves out every path with a segment that starts with a dot
(`.github`, `.agents`, `.claude`, `.eslintrc.js`) and tests: test source
sets (Maven's and Gradle's), `src/test` directories, `test`, `tests`,
`__tests__` and `__mocks__` directories outside Java (where a package may be
named `test`), and `test_*.py`, `*_test.py`, `conftest.py`, `tests.py`,
`*.test.*` and `*.spec.*` files. The build warnings of the test source sets
left out are not reported either. `CODEGRAPH_DISCOVERY_INCLUDE_HIDDEN=true` and
`CODEGRAPH_DISCOVERY_INCLUDE_TESTS=true` analyse them too. Both are part of
the analysis configuration digest, so changing them recomputes every
repository on its next run and retires the files now left out.

## Run

From the repository root:

```sh
make env           # creates apps/forge-codegraph-worker/.env, and the code graph's shared settings in .env.common
make up            # the whole stack, the worker and its Spanner emulator among it
make logs-worker
curl -s localhost:18090/readyz
```

Set `FORGE_GITHUB_TOKEN` in `.env.common` first to ingest private
repositories ([GitHub access](#github-access)), and `OPENAI_API_KEY` to
embed. The worker claims its jobs from `admin-mysql` (Compose starts it
first). It stores the graph in `worker-spanner`, a Spanner
emulator on `127.0.0.1:19030` that the worker provisions (instance,
database, schema) on start. Emulator data lives in memory and is lost when
the container stops; [managed Cloud Spanner](#on-managed-cloud-spanner)
keeps it.

Natively, against the same emulator:

```sh
make worker-install    # builds python-analyzer and typescript-analyzer and downloads Go modules
make admin-migrate     # once: the admin MySQL's tables, the job queue among them
make worker            # starts worker-spanner and admin-mysql, then go run ./cmd/codegraph-worker
```

The worker stops at startup when the admin MySQL has no
`code_ingestion_jobs` table yet, saying to run `make admin-migrate`.

The worker's host ports sit beside `forge-aidlc-parent`'s, so both stacks
run at once: health and admission on 8090 natively and 18090 in Compose
(`WORKER_HEALTH_PORT`), the emulator on 19030 (gRPC, `SPANNER_GRPC_PORT`)
and 19040 (REST, `SPANNER_HTTP_PORT`).

The process accepts no arguments. It reads defaults, then `codegraph.yaml`
when present, then `.env` from its working directory (`CODEGRAPH_ENV_FILE`
selects another file), then the process environment; `.env.example` lists
every setting. Values the worker shares with other apps (the admission token,
the cursor signing key, the GitHub token, the embedding model, dimensions and
gateway every app embeds with (`FORGE_EMBEDDING_*`) and the OpenAI key) live
once in the repository's `.env.common`, and `.env` names each
with a `${NAME}` reference, e.g.
`CODEGRAPH_GITHUB_TOKEN=${FORGE_GITHUB_TOKEN}`. A reference
resolves to an earlier line of `.env`, otherwise to `.env.common`
(`../../.env.common`, or `FORGE_ENV_COMMON_FILE`; empty disables it); a name
defined in neither stops startup unless the process environment sets that
variable, and nothing in `.env.common` reaches the environment unless `.env`
references it. The container has no `.env.common`: Compose passes the shared
values it needs. Scratch data (git mirrors, the syntax cache, run indexes)
defaults to `.codegraph-work` in the working directory and is disposable.
Java needs a JDK and, for `maven-resolved`, Maven; the image ships both. For
native runs, point the Java entry of `CODEGRAPH_LANGUAGES` in `.env` at a
local JDK and Maven (`maven_executable`) and set
`CODEGRAPH_BUILD_MODE=maven-resolved`: in `auto` mode no JDK or dependencies
are resolved, so a repository with Java sources fails at the resolve stage.
Without Java in `CODEGRAPH_LANGUAGES`, Java files are left out and the other
languages still ingest. The JDK must contain no symlinks, since the
worker fingerprints it and refuses them; macOS JDKs have some, so copy one
with `cp -RL <jdk>/Contents/Home ~/.codegraph/jdk`, as the image does.
Old Java 8 projects often fail to build on a new JDK; `build_java_homes`
(`{"8":"/abs/jdk8"}`) lets Maven build them with the JDK they target, and
an installed JDK works as is ([Java Maven inputs](docs/java-maven-inputs.md#build-jdks)).
Sources Maven can't compile no longer fail the run: the code graph compiles
them itself for the code that depends on them when they compile there (as
when only the build's JDK or Lombok was the problem); otherwise it succeeds
with a warning, and references into them stay unresolved
([Java Maven inputs](docs/java-maven-inputs.md#sources-javac-rejects)). Lombok
runs during resolution for projects that use it; a project's Lombok older
than the JDK (1.18.32 on JDK 24) can't, so set `lombok_jar` to a newer one
(`"lombok_jar":"/abs/lombok-1.18.38.jar"`) or its generated members stay
unresolved. The image doesn't ship one yet.

### Following a run in the logs

Each run logs a short sequence of lines, all carrying `run_id` and
`repository_id`: `run started`, then `phase started` and `phase finished`
for each phase, then `run finished` (or `run stopped`, naming the phase it
stopped in). Every phase line has `phase` and `step` (its place in a full
run): `prepare` 1/11, `fetch`, `build`, `discover`, `parse` (source to IR),
`resolve` (relationships), `match` (persistent IDs), `load` (adding to the
graph), `verify`, `publish`, `embed` 11/11. A `phase finished` line carries
the phase's `elapsed` time and counts. While `parse`, `resolve`, `load` or
`embed` runs, a `phase progress` line reports `done`, `total`, `left`,
`percent` and `eta` at most every 20 seconds, so a short phase logs only its
start and finish. Resolution reports a file as done when its language's
resolver stores its references: Java does so file by file, Python one
context at a time and TypeScript at the end of its pass. To follow one run:

```sh
make logs-worker | grep '"run_id":"<run id>"' | grep -E '"msg":"(run|phase|language)'
```

Build the image from the repository root; the context must include
`packages/`:

```sh
docker build -f apps/forge-codegraph-worker/Dockerfile .
```

### On managed Cloud Spanner

The emulator keeps everything in memory. To keep the graph, run the worker
on a Cloud Spanner database instead, natively or in Compose:

1. An instance on the Enterprise edition or higher: the schema has a
   full-text search index and a vector index. Creating one bills from the
   start, so nothing here does it for you; for example,
   `gcloud spanner instances create codegraph --edition=ENTERPRISE --config=regional-us-central1 --processing-units=100 --description=codegraph`.
2. Name the database in `.env.compose`:
   `WORKER_CLOUD_SPANNER_DATABASE=projects/<project>/instances/<instance>/databases/codegraph`.
3. Create it with the codegraph schema, as your gcloud user (it needs the
   Spanner database admin role): `make worker-spanner-cloud`. The vector
   length comes from `CODEGRAPH_EMBEDDING_DIMENSIONS` in `apps/forge-codegraph-worker/.env`,
   which names the shared `FORGE_EMBEDDING_DIMENSIONS` in `.env.common`
   (3072 when unset), and is fixed for the database's life.
   `make worker-spanner-cloud-reset` drops and recreates it. When a newer
   schema adds tables or indexes (such as `CGCrossLinks`),
   `make worker-spanner-cloud-update` shows what the database lacks and
   adds it after you confirm, keeping everything in it; it never alters or
   drops.
4. Run the worker on it:
   - natively, `make worker SPANNER=cloud`, as your Application Default
     Credentials (`gcloud auth application-default login`) or the key in
     `GOOGLE_APPLICATION_CREDENTIALS`;
   - or in Compose, `make up SPANNER=cloud`, with a credential file at
     `CLOUD_SPANNER_KEY_FILE` in `.env.compose` (default
     `./.secrets/spanner-sa.json`): a service-account key, or the absolute
     path of your `~/.config/gcloud/application_default_credentials.json`.
     Pass `SPANNER=cloud` to `restart`, `logs`, `status` and `down` too.

The worker itself needs only the Spanner database user role on the
database. With `SPANNER=cloud` it never provisions anything and the
emulator isn't started. `compose.cloud.yaml` is the Compose overlay; it
also turns off the client's metric export to Cloud Monitoring, which a
Spanner-only credential can't write.

## Tests

```sh
make worker-check          # go vet and unit tests for the worker and packages/go/code-graph
make worker-test-python    # builds python-analyzer; its tests, then the Go Python tests
make worker-test-typescript  # builds typescript-analyzer; its tests, then the Go TypeScript tests
make worker-test-spanner   # starts worker-spanner; storage and pipeline integration tests
make worker-test-mysql     # starts and migrates admin-mysql; the job queue's integration tests
CODEGRAPH_TEST_JAVA_HOME=/abs/jdk CODEGRAPH_TEST_MAVEN=/abs/mvn make worker-test-java
```

One module on its own, as the Docker build resolves it:

```sh
cd apps/forge-codegraph-worker
GOWORK=off GOTOOLCHAIN=auto go test ./...
```

The pipeline integration test needs a JDK as well as the emulator:

```sh
make worker-deps
SPANNER_EMULATOR_HOST=127.0.0.1:19030 CODEGRAPH_TEST_JAVA_HOME=/abs/jdk CODEGRAPH_TEST_MAVEN=/abs/mvn \
  GOTOOLCHAIN=auto go -C apps/forge-codegraph-worker test -tags=integration ./internal/ingestion -run TestPipelineGenerations -v
```

It publishes five generations of a Maven fixture and checks
implements/overrides edges, history and as-of reads. See
[docs/language-routing.md](docs/language-routing.md) for how the
adapters compose and [docs/architecture.md](docs/architecture.md) for
generations, incremental scope and identities. The docs describe the original
`ei-aitiger-codegraph` system, which also has admin, API and MCP services
that read the same graph.

## Completion and retries

With embeddings enabled, a published graph is still `RUNNING` until its
required semantic index is `COMPLETE`. Embedding failure records `FAILED` /
`INCOMPLETE`; the job can't succeed with it. Retries
resume missing or stale vectors on the existing generation. Already committed
vectors are reused, including a partially committed page.

Automatic retries use a conservative allowlist:

- Retry temporary service unavailability, transaction aborts, dependency
  timeouts, connection resets, interrupted responses, lease loss, and provider
  HTTP 408/409/429/500/502/503/504.
- Stop on bad input, credentials, permissions, unsupported capabilities,
  integrity failures, malformed embedding responses, exhausted billing quota,
  panics, and unclassified errors. A whole-job deadline is a workload limit and
  stops the job; a dependency timeout can retry.
- A permanent error wins when combined with cancellation or transient cleanup
  errors.

A permanent failure fails the job (`FAILED`), then its unfinished run.
Transient failures requeue the job with backoff, 30 seconds doubling to ten
minutes, and use up an attempt; after `CODEGRAPH_MAX_ATTEMPTS` (6) the job
fails with `retry_exhausted`. A lapsed claim used up its attempt too; when it
was the last, the next worker only settles the job, from its run: done if
the run ended, failed if not. A busy repository and a worker shutting down
requeue the job without using one up. Claim tokens only grow. Retrying a
failed ingestion (the admin API's retry, after the cause is fixed) resets
its attempts, keeps its commit and resumes its run.

The embedding HTTP client makes up to eight attempts for transient responses
and for requests that got no complete answer (a reset connection, a timeout, a
truncated body), backing off from one second to a minute with jitter, or as
long as the provider asks through `retry-after-ms`, `Retry-After` or
`x-ratelimit-reset-*`; a requested wait over five minutes ends the request
with its retryable error, so the job is retried later instead. A request the
provider refuses for its content (HTTP 400, 413, 422) is split in halves
until the refused text is alone: that one document is left without an
embedding, the run says how many were, and the next ingestion tries them
again. A provider that refuses even a trivial input is misconfigured and the
pass fails. Quota exhaustion is a permanent error even when the
provider uses HTTP 429, as distinguished in the
[official OpenAI error documentation](https://developers.openai.com/api/docs/guides/error-codes).
Unclassified Git/Maven subprocess exits stop automatically; those exit codes
alone do not establish that another attempt could succeed.

Existing successful records with an incomplete index are resumable through
the pipeline, by ingesting the repository again.

## What degrades instead of failing

A repository is ingested with what can be analysed; what could not is left
out, recorded, and reported on the run as a warning (the admin console's
"Ingested with gaps"), never silently. A run fails only for a defect
(an integrity violation), an unusable configuration of the worker itself, or
infrastructure it cannot reach.

| Stage | What goes wrong | What happens |
| --- | --- | --- |
| Checkout | the branch was force-pushed | the run diffs against the published commit's files whatever their ancestry; an older commit is still superseded |
| Checkout | the published commit is gone | every file is recomputed, and files the commit no longer has are retired |
| Build (Maven) | a plugin fails, a module does not compile, dependencies cannot be resolved | the stage runs again past the failures, one module at a time with more heap when the failure was not javac's; modules left without classes or classpath become gaps naming the failed step; fragile plugins that write nothing javac reads (enforcer, GPG, Docker/Jib, git-commit-id, license, frontend/npm, …) are skipped up front |
| Build (Maven) | no pom.xml at the root | the one top-level project is built with `-f`; several are built by one aggregator reactor |
| Build (Maven) | fixture POMs, duplicate coordinates, system-scoped JARs, `type=pom` dependencies, timestamped snapshots, a JAR outside the checkout | placed where they can be, left out with a gap where they cannot |
| Build (Maven) | Java level 6/7 or newer than the JDK | analysed at the nearest level the JDK compiles |
| Build (Python) | an unreadable `pyproject.toml` or `pyrightconfig.json`, a `.python-version` outside 3.10–3.13 or listing several, conflicting targets | the setting is left out or brought inside the supported range, with a `configuration` gap saying so |
| Build (Python) | a declared package the index does not have or has no wheel for, a URL or VCS requirement | the others are installed and it is named in a gap; code using it resolves to its dotted name |
| Build (TypeScript) | a package from a private registry, git or a tarball, or one the registry does not have | the others install and it is named in a gap; code using it keeps its syntax-tier binding by name |
| Build (TypeScript) | the registry cannot be reached, npm runs out of time, the installation grows past its budget | the project is analysed without packages, with a gap; installing is tried again six hours later |
| Build (Python) | the package index cannot be reached, installation times out or grows past its budget | the last installation of those requirements is used, or none, with a gap; installation is tried again six hours later, not on every run |
| Build (any) | the language's build cannot be read at all | its sources are analysed with the syntax profile, with a `build` gap |
| Discovery | a name that is not UTF-8 or has control characters, a tree nested past the depth limit | that path is left out and listed |
| Parse | a file in a single-byte encoding (Latin-1, Windows-1252) | parsed with those bytes read as `?`, one for one, so spans still address the file |
| Parse | a file past a parser limit, a parser crash or a two-minute parse | that file is left out (its last parsed facts stay) and listed; the parser is replaced after a crash |
| Parse | more syntax errors than the diagnostic budget | the issues are cut, not the file |
| Resolve | javac crashes on a file, a context runs past its time, an annotation processor fails | the file is quarantined and the context compiled again without it; what cannot be compiled keeps its declarations with unresolved references; the context is resolved again on the next run |
| Resolve | a language's resolver fails | that language's references are unresolved in this commit, the others resolve, and its contexts are resolved again on the next run |
| Resolve | the TypeScript compiler is not built, crashes, or runs out of heap or time | the references it had not bound keep their syntax-tier bindings, with a warning, and the context is resolved again on the next run |
| Project | invalid UTF-8 or NUL in a name, text or diagnostic; very long names, signatures or candidate lists; two facts under one ID | text is cleaned and bounded; the first fact wins and the rest are counted |
| Load | a batch of very large records | commits are bounded by bytes as well as records |
| Embed | see above | refused documents are skipped and counted |
| Worker | a deploy changes the analysis configuration | queued jobs of a configuration no live worker serves are adopted |
| Worker | a crashed run left scratch behind | worktrees, run indexes and build scratch older than a day are removed at startup |
| Worker | a panic in a run's goroutine | that run fails; the worker and its other runs go on |
| Worker | the repository lease cannot be renewed until it expires (the database unreachable or overloaded) | the run stops with the lease loss as its failure, and the job is retried |

