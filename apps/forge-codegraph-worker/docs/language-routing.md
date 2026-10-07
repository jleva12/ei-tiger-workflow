# Language adapters and routing

How the worker composes a parser, a build provider and a binding resolver per
language, which build modes can complete a run, and what bundling another
language requires. Three adapters are bundled: Java, bound by javac,
TypeScript/JavaScript, bound by a syntax-tier resolver, and Python, bound by
a pinned Pyright process ([Python ingestion](python-ingestion.md)).

## Composition at startup

`workerapp.Bootstrap` builds the bundled registry
([`internal/languages/builtin/registry.go`](../internal/languages/builtin/registry.go)),
then asks it for two things:

1. `Registry.Resolve(BuildConfig{Mode, Manifest}, Languages, ParserLimits)`
   returns the parser registry for the selected languages and one
   `buildcontext.Provider` for the build mode.
2. `Registry.Resolvers(mode, Languages)` returns each selected language's
   `semantic.Resolver`. Bootstrap keeps them all; the pipeline routes every
   compilation context to the resolver of its source set's language
   (`SourceSet.Language`, Java when empty), so one process binds every
   language it has an adapter for.

The resulting `Dependencies` (dispatcher, build provider, resolvers by
language) are injected into `ingestion.New`. The same dispatcher classifies
files during discovery and parses them, so extension mappings cannot diverge.
The analysis configuration digest binds every admitted run to the dispatcher
digest, every language's resolver version and policy, and the parser, build
and discovery limits; changing any of them, including adding a language,
means new admissions. The worker registers the same list of languages in
`CGWorkers`, which the admin API reports.

## Configuration

`CODEGRAPH_LANGUAGES` is a JSON object of language ID to adapter settings.
`{}` enables every bundled adapter with its defaults; a nonempty object
selects only the named languages. Each adapter decodes and validates its own
namespace; unknown adapters, unknown fields and unsupported values fail
startup. The process environment overrides the env file and replaces the whole
object.

The Java namespace ([`internal/languages/java/config.go`](../internal/languages/java/config.go)):

| Field | Meaning |
|---|---|
| `java_home` | Absolute, symlink-free JDK directory. Required: javac is the binding authority. |
| `maven_executable` | Absolute path to `mvn`; required for `maven-resolved`. |
| `cache_dir`, `work_dir` | Service-owned absolute directories for the Maven repository, verified compiler inputs, the compiled bridge and javac scratch. |
| `max_heap_mib`, `parallelism` | Heap of the single javac process and contexts attributed concurrently inside it. |
| `fallback_release` | Extraction profile used only when declaration discovery cannot infer a release (8, 11, 17 or 21). |
| `release` | Explicit profile for `syntax` mode only. |
| `build_java_homes` | Older JDKs by major version that Maven may build old projects with ([java-maven-inputs.md](java-maven-inputs.md)). |
| `lombok_jar` | Optional absolute path to a Lombok JAR that runs on `java_home` (1.18.38 or later for JDK 24). The resolver runs Lombok for source sets that have it on their classpath, their own JAR first; this one is used when that one cannot run on `java_home`. Its digest is part of the resolver's policy digest. |

The TypeScript namespace ([`internal/languages/typescript/config.go`](../internal/languages/typescript/config.go))
covers TypeScript and JavaScript files alike (`.ts`, `.tsx`, `.mts`, `.cts`,
`.js`, `.jsx`, `.mjs`, `.cjs`):

| Field | Meaning |
|---|---|
| `version` | TypeScript language level the syntax profile parses under, `4` or `5` (default `5`). |
| `excludes` | Extra relative source globs the walk skips (`dev/browser/**`), in addition to `node_modules`, every tsconfig `outDir` and the defaults. |
| `includes` | Relative source globs that restrict the walk to matching files; exclusions still take precedence. |
| `default_excludes` | `false` switches off the built-in build-output list: `**/target/**`, `**/dist/**`, `**/build/**`, `**/out/**`, `**/.next/**`, `**/.nuxt/**`, `**/.output/**`, `**/coverage/**`, `**/.cache/**`. Default `true`. |
| `compiler` | The compiler tier: the TypeScript compiler binding the same sites over each project's tsconfig; on when `node` and the built bridge are found ([typescript-compiler.md](typescript-compiler.md)). |
| `install` | Installing each npm project's packages, with every script off, for the compiler to read; on with the compiler when `npm` is found ([typescript-compiler.md](typescript-compiler.md#installed-packages)). |

Build output is generated from sources already in the graph and is never
bound, so it is excluded by name, and the parser also declines a file it
recognizes as generated or minified (a generation marker in its first 4 KiB,
a source-map comment at its end, or a mean line length in the thousands)
with `parser.ErrGeneratedSource`; the parse stage skips and logs such a
file with its reason instead of failing the run.

Its `Discover` provider ([`internal/languages/typescript/project`](../internal/languages/typescript/project/provider.go))
runs in every build mode and, without the compiler tier, executes nothing
(with it, it installs each npm project's packages with every script off;
see [typescript-compiler.md](typescript-compiler.md)): it reads every
`tsconfig.json`/`jsconfig.json` (with `extends`, comments and trailing
commas) for `baseUrl`, `paths` and `outDir`, and the root `package.json`
workspaces or `pnpm-workspace.yaml` for the packages importable by name with
their entry files. The result is one repository-wide `typescript` source set
whose language options carry those projects and packages for the resolver
and whose exclude patterns skip `node_modules` and every project's output
directory; the discovery walker prunes those trees without visiting them. A
checkout with no such file reports `ErrNoBuild` and gets the plain syntax
fallback instead. Because the options are part of the source set's digest,
a tsconfig or workspace change re-resolves the set like a dependency change
does for Java.

```dotenv
CODEGRAPH_BUILD_MODE=maven-resolved
CODEGRAPH_LANGUAGES='{"java":{"fallback_release":21,"java_home":"/opt/java/codegraph-jdk","maven_executable":"/opt/maven/bin/mvn","cache_dir":"/var/lib/codegraph/java/cache","work_dir":"/var/lib/codegraph/java/work","max_heap_mib":2048},"typescript":{},"python":{}}'
```

## Build modes and which complete a run

`CODEGRAPH_BUILD_MODE` selects the provider the registry returns:

| Mode | Provider | Context status | Attributable by the Java resolver |
|---|---|---:|---|
| `maven-resolved` | [`internal/languages/java/maven`](java-maven-inputs.md): runs Maven, materializes and fingerprints every compiler input | complete | Yes. This is the mode Compose and production use. |
| `auto`, `maven`, `gradle` | [`internal/buildcontext/discover`](build-discovery.md): reads build declarations, executes nothing | incomplete | No: dependencies are unavailable artifacts and the JDK is unknown. |
| `manifest` | [`internal/buildcontext/manifest`](build-context.md): explicit `.codegraph/build-context.json` | complete or incomplete | Only when every binary input lives under the roots the resolver mounts: `checkout`, `java-jdk` and `java-cache`. |
| `syntax` | `internal/buildcontext/syntax`: one profile per language, repository-wide roots | incomplete | No. |

The resolver refuses a source set without a pinned JDK matching `java_home`,
a classpath entry that is missing, or an input whose fingerprint does not
match the inventory. A run in one of the incomplete modes therefore fails at
the resolve stage with an explicit error; there is no syntax-only graph. The
worker default is `auto` for local experiments with discovery; ingestion that
publishes needs `maven-resolved`.

In every other mode the registry composes the enabled languages
([`internal/buildcontext/composite`](../internal/buildcontext/composite/provider.go)):
each language's own `Discover` provider describes its source sets, and the
inventories are merged into one sealed context whose producer records which
provider described each language. A language with no `Discover` factory, or
whose provider reports `buildcontext.ErrNoBuild` (Maven: no `pom.xml` anywhere
outside `.git` and `target`),
is covered by its adapter's syntax profile over the whole checkout, so every
registered file is discovered even when no build declares it. Declared source
sets always win: the fallback is created only for a language that has none,
and the walker emits a file only for sets of the file's own language, so a
Java file is never claimed by another language's fallback. The composed
context is `incomplete` whenever a fallback is present; the javac resolver
still refuses a Java fallback set, so a Java repository without a Maven build
fails at the resolve stage as before. A repository with a Maven API and a
TypeScript front end therefore yields Maven's Java sets plus the TypeScript
syntax fallback set, each bound by its own resolver in the same generation:
javac for the Java sets, the syntax-tier resolver for the TypeScript set.

## The TypeScript and JavaScript adapter

The [parser](../internal/parser/typescript/README.md) uses the
upstream Tree-sitter `typescript`, `tsx` and `javascript` grammars and maps
the language onto `pkg/ir`: module-level variables, type aliases and
namespaces are declaration kinds of their own, imports carry their written
module specifier, decorators are annotations, arrow functions and function
expressions are lambda sites, object literals and JSX are expression kinds,
and function, structural, literal and operator types are type kinds whose
nested types are `component` uses.

The [resolver](../internal/resolve/typescript/README.md) binds
from the source set's own files: lexical scopes, imports resolved against
discovered files (relative paths, `tsconfig` path mappings and `baseUrl`,
workspace package names, with extension and index completion) and followed
through re-exports and barrel files, `this`, declared and constructed types
followed one member at a time, inherited members, overrides and heritage.
An unchanged file whose IR left the syntax cache is parsed again from the
checkout so its exports stay followable. Every lookup it writes carries
`provenance: syntax`, which the projector copies onto the edge, so a
TypeScript `calls` edge is distinguishable from a javac-attributed one.
Package specifiers and runtime-library members are unresolved with cause
`external_dependency`; everything the tier cannot prove is unresolved with
cause `analysis_limitation`, never guessed. The
[compiler tier](typescript-compiler.md) then binds the same sites with the
TypeScript compiler, each file under the tsconfig that governs it and with
the project's packages installed, and every binding it proves replaces the
syntax tier's with `provenance: compiler`.

## Adapter contract

A bundled language provides a `languages.Adapter`:

```go
type Adapter struct {
    Parser    parser.Registration
    Configure func(raw json.RawMessage, buildMode string) (Configured, error)
}
type Configured struct {
    SyntaxProfile syntax.Profile
    Discover      func(mode string, inputs manifest.Config) (buildcontext.Provider, error)
    Resolver      semantic.Resolver
}
```

`parser.Registration` carries the `Descriptor` (lowercase language ID, exact
filename extensions including the dot, adapter version, grammar version and
extraction feature set), a `New` session factory, `Validate` for profiles and
limits, and `WorkerMemory`, the per-session memory estimate the worker uses to
admit parser workers. See the [parser contract](parser-contract.md) and the
[Java registration](../internal/parser/java/registration.go). `Configure`
returns the resolver that proves bindings for that language; the pipeline
never guesses a target from syntax. Register the adapter in
`builtin.Registry()`; nothing else in the worker changes.

`parser.NewRegistry` rejects duplicate languages and conflicting extensions.
Matching is on the final extension only; compound suffixes, shebangs and
plugins loaded from disk are not implemented.

## Discovery and parsing

Discovery walks the source and generated roots of every source set in the
build context and emits one identity per file variant. A file is emitted only
for source sets whose language matches its registered extension; a registered
file of another language inside a set counts as `other_language_files`, an
unregistered extension as `unsupported_files`. Counts are visits across
compilation contexts, not unique paths.

Every source set's `language`, `language_version` and `language_options`
become a `parser.Profile`, validated by the dispatcher before parsing starts.
The parse stage runs `CODEGRAPH_WORKERS` workers, reduced so that
`WorkerReservation` times the worker count fits `CODEGRAPH_WORKER_BUDGET_BYTES`.
Each worker owns one parser session. Parsed IR goes to the persistent syntax
cache keyed by content hash, parser descriptor and profile, so an unchanged
file is never parsed twice across runs. A file above `CODEGRAPH_MAX_SOURCE_BYTES`
or one that exceeds a structural limit is skipped, counted and named in the
log with its reason; it stays discovered but contributes no declarations or
bindings, and a resolver reports it as `skipped`. Dispatch and I/O failures
abort the run.

## Bundling a second language

The registry, dispatcher, discovery walker, build composition and pipeline
stages are language-neutral: `Bootstrap` keeps one resolver per language,
`ingestion.Config.Resolvers` routes each compilation context by
`SourceSet.Language`, the syntax cache keys IR by language, and the analysis
digest and the worker registration list every language. The TypeScript
adapter is the worked example: a parser registration that maps the language
onto `pkg/ir` (extending it only where a construct had no home), a
`Configure` that returns the syntax profile and the resolver, and a
resolver that labels its facts as syntax-derived rather than proven. A
language with a compiler-grade binder plugs it in as its resolver instead.
Python now has a Tree-sitter adapter and pinned Pyright semantic resolver;
see [Python ingestion](python-ingestion.md). TypeScript has a compiler tier
over its syntax tier ([typescript-compiler.md](typescript-compiler.md)). Go,
C# and Groovy are tracked in [closing the breadth gap](gap-closure.md).
