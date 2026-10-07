# Java Tree-sitter adapter

`java.New()` returns the concrete implementation of `pkg/parser.Parser`. It extracts an `ir.SourceFile` from the supplied UTF-8 bytes without opening the source path, selecting semantic targets, synthesizing framework members, or writing graph records.

## Design

The adapter is a Tree-sitter frontend with a small, vendored grammar patch for annotated varargs, multi-catch annotations, patterns, `case null, default` and module providers:

| Component | Pinned version/design |
| --- | --- |
| Go bindings | `github.com/tree-sitter/go-tree-sitter v0.25.0` |
| Java grammar | `v0.23.5-ei.1`, patched from upstream `v0.23.5` ([provenance](../../../../../packages/go/code-graph/tree-sitter-java/README.md)) |
| Grammar and queries | Process-wide immutable cache initialized once |
| Extraction queries | Embedded `definitions.scm`, `calls.scm`, `imports.scm`, `references.scm` |
| Native parser | One reusable session per worker; never used concurrently |
| Tree and query cursors | Owned and released within each parse |

Query captures select extraction categories. Source-ordered traversal of Tree-sitter nodes and named fields supplies the richer IR details. The queries cover additional declaration forms, capture whole imports, and distinguish callable references from invocations. SCM queries are structural; initialization rejects predicates because native positions use UTF-16. Text comparisons use the translated-source mapper. Unicode translation precedes native parsing, and every returned span maps back to the original UTF-8 bytes. No regex parser or JVM subprocess replaces Tree-sitter; the regression fixtures assert syntax facts, never binding targets.

Calls on one `Parser` serialize through cancellable admission. Use separate instances for parallel workers; they share the grammar and compiled queries. Call `Close(ctx)` when a worker finishes. It is idempotent, waits for an admitted parse, and releases the native parser. Parsing a closed instance returns `ErrClosed`. Instances must be constructed with `New`; the zero value is not usable.

The pinned binding retains a Go handle when non-nil `ParseWithOptions` options are supplied. This adapter uses its cancellation-flag API with a pinned scalar and a joined watcher, resets the native session after parsing, and releases the tree on every return path. See the [upstream binding implementation](https://github.com/tree-sitter/go-tree-sitter/blob/v0.25.0/parser.go) and [reported options-handle leak](https://github.com/tree-sitter/go-tree-sitter/issues/55). This should be revisited when upgrading the binding.

## Usage

The following belongs inside this Go module because the adapter is an `internal` package. The public interface and IR remain reusable independent contracts.

```go
engine, err := java.New()
if err != nil {
    return err
}
defer engine.Close(context.Background())

// source must carry the exact size/SHA-256 of content and explicit java/release.
file, err := engine.Parse(ctx, parser.Input{
    Source: source,
    Content: content,
    Limits: parser.DefaultLimits(),
})
if err != nil {
    return err // no artifact; errors.Is identifies limits/input/cancellation
}
// Check file.Coverage before admitting facts to the next phase.
```

Build with Go 1.25+ and a C compiler (`CGO_ENABLED=1`). A JDK is not required to extract syntax; the [javac resolver](../../resolve/java/README.md) needs one, together with the build inventory, to bind what this adapter extracts.

## Extraction feature set: `java-syntax/2` (adapter `0.2.0`, IR `1.1.0`)

Implemented facts include packages; all four traditional import forms; classes, interfaces, enums, records, annotation types, local/anonymous types; methods and normal/compact constructors; fields, locals, parameters, receivers, record components and type parameters; explicit modifiers, adjacent Javadocs and initializer blocks; written qualified/generic/array/wildcard/intersection/union types; heritage, bounds, throws and type uses; annotation identity, keys, arrays, nested annotations and unevaluated values; lexical scopes for blocks, lambdas, loops, catches and resources; expressions, complete receivers, ordered arguments, construction/delegation, separate callable references, and name/member read/write occurrences. The extractor now also preserves statements, block-lambda bodies, return/yield expressions, control-flow syntax, type/record patterns, switch labels/guards/arms, module directives, diamond syntax and annotation-element defaults. See the [syntax support boundary](../../../docs/java-syntax-support.md).

IDs and table order are deterministic for identical input/configuration/producer versions. IDs are file-local extraction identities, not stable symbols across edits. Tables contain forward links where necessary; their order is deterministic but is not a promise of lexical sorting. Names/operators reflect Java Unicode translation; exact spellings, numeric/string lexemes and byte spans preserve the original source. Supplementary characters and lone UTF-16 surrogate escapes in literals survive native parsing. Lines are one-based; columns are zero-based byte columns. CRLF, LF, lone CR and BOM bytes retain their original offsets.

Important representation choices:

- Type/callable scopes include their declaration headers so that type parameters, parameters, bounds and return types have a containing scope. Method body blocks receive child scopes. `BodyScopeID` points to the declaration's type/callable scope.
- Resource scopes have explicit regions for the resource specification and try body. Catch/finally scopes attach to the outer environment. Enhanced-for iterable expressions are also extracted in the outer environment.
- Post-name array suffixes (`int a[]`, `int f()[]`) retain the exact suffix span/spelling and link the leading type through `ElementTypeID`. Consumers follow that link instead of treating the fragment spelling as a canonical type.
- Array construction retains written dimensions and their annotations in a type record; dimension-size expressions and the initializer are source-ordered expression operands.
- Annotations shared by multiple declarators are attributed separately to each owner. Their spans identify the shared syntax. Declaration annotations are not classified by Java `@Target` until binding supplies annotation metadata.
- Pattern variables use a distinct declaration kind. Their containing scope is a lexical anchor; successful-match flow determines visibility later. Conditions, negation, short circuiting, branch bodies, loop headers and early exits remain linked syntax inputs, without inventing flow-visible intervals.
- Callable and initializer `BodyStatementID` links identify executable blocks. Lambda block bodies have both scope and statement links. Switch arms retain ordering, arrow/colon form, guards, patterns, constants, default and yield syntax. Completion, exhaustiveness and target typing are binder work.
- `TypeSegment.ArgumentSyntax` distinguishes absent, explicit and diamond type arguments. Annotation-element defaults use `Callable.AnnotationDefault`, an expression/array/nested-annotation union.
- Module directives preserve annotations, names, modifiers, targets and service/provider type uses, including every provider. Build inventory supplies module-path visibility.
- Type qualifiers in callable references use `TypeUseCallableReference`; references remain deferred syntax. Variadic record components retain `Variable.Variadic`. These are additive values in the initial IR contract.

## Coverage and limits

Release profiles are explicit strings `8`, `11`, `17`, or `21`. Preview options, other releases and unsupported budgets return `parser.ErrUnsupportedConfig`. Recognized newer syntax (records, sealed types, text blocks, switch rules and patterns) receives release diagnostics when it exceeds the selected profile. `LanguageValidation` records `not_checked` with method `java-syntax-profile`, or `invalid` for identified release/Unicode violations. This is separate from extraction coverage. Full compiler/build validity must be recorded by a later phase; `complete` never certifies compilation. The exact release-check matrix is in the [syntax support boundary](../../../docs/java-syntax-support.md).

The adapter reports partial/failed coverage for syntax recovery and unsupported constructs. Preview/template syntax remains unsupported. This does not claim exhaustive grammar coverage; binding is the resolver's job. Unknown expressions/types preserve available syntax and carry coverage issues. An unrecognized statement is reported rather than silently treated as complete.

Configured source budgets up to 16 MiB and syntax depths up to 256 are supported. Every limit must be positive. Source bytes are checked before translation/native parsing. Translation retains UTF-16 units and a mapping to original byte boundaries, so its auxiliary memory is proportional to input bytes. Native parsing honors context cancellation; an iterative completed-tree scan enforces node/depth budgets before queries and IR extraction. Record and combined diagnostic/coverage budgets are checked as records are produced. Serialized records are accounted before retention, followed by a complete JSON-envelope check. Exceeding a hard limit returns zero IR with `ErrLimitExceeded`.

Tree-sitter allocates its native tree before node/depth counts are available. These limits are not a native-memory cap; temporary serialization buffers are also outside the retained-output accounting. The worker bounds parser concurrency with `CODEGRAPH_WORKER_BUDGET_BYTES` divided by this adapter's `WorkerMemory` estimate; it does not isolate native memory per process. See the [public parser contract](../../../docs/parser-contract.md) for exact budget meanings.

## Verification

```sh
go test ./apps/forge-codegraph-worker/internal/parser/java
go test -race ./apps/forge-codegraph-worker/internal/parser/java
go test ./apps/forge-codegraph-worker/internal/parser/java -run='^$' -fuzz=FuzzParse -fuzztime=20s -parallel=4
make check
# Optional independent fixture compilation; the JDK must support --release 21:
JAVAC=/abs/jdk/bin/javac go test ./apps/forge-codegraph-worker/internal/parser/java -run TestRequirementCompilerFixtures
# Reproduce the committed grammar with Tree-sitter CLI 0.25.0 and Python 3:
sh packages/go/code-graph/tree-sitter-java/generate.sh --check
```

Tests independently assert extraction on real Java fixtures, original-engine fixture compatibility, scope restoration, complete receiver chains and qualified `super`, constructor/reference distinctions, generic segment association, post-name dimensions, annotation values, resource visibility, deterministic output, source ownership/positions, release diagnostics, hard limits, cancellation/reuse, native session separation and malformed-input recovery. The hand-authored `pkg/ir/testdata/parsed_file.json` is not used as a parser oracle.

The optional compiler suite checks 16 valid fixture groups under `--release 21` and two expected Java 8 rejections. The regular suite asserts typed required/forbidden facts and table integrity/budgets. Compiler acceptance is a fixture oracle only; neither test suite asserts resolved call targets.

Java source profiles retain validated compiler/build metadata in the options
fingerprint: `java.compiler.mode`, `java.compiler.target`, `java.compiler.proc`,
`java.maven.execution`, and `java.maven.phase`. These settings describe the
pinned build context; they do not enable parser syntax or annotation processing.
Unknown keys and malformed values remain unsupported configurations.
