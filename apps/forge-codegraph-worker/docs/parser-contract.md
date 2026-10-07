# Parser contract

[pkg/parser](../../../packages/go/code-graph/domain/parser/parser.go) is the public boundary for syntax extraction:

```go
type Parser interface {
    Parse(ctx context.Context, input Input) (ir.SourceFile, error)
}
```

The public package implements the interface, typed inputs/options/budgets, errors, and input validation. The concrete [Java adapter](../internal/parser/java/README.md) and [TypeScript/JavaScript adapter](../internal/parser/typescript/README.md) implement extraction. Discovery (`internal/discovery`), binding (`internal/resolve/java`, `internal/resolve/typescript`) and projection (`internal/graphanalysis`) consume their output and never feed back into it. The package depends only on the standard library and `pkg/ir`.

The public package also supplies an immutable `Registry`, adapter `Registration` and worker-owned `Session` lifecycle contract. See [language routing](language-routing.md) for registration, configuration and discovery.

## Input and ownership

`Input.Source` reuses `ir.Source` for file, repository, snapshot, path, content hash/size, language/version, and optional build/module/source-set/artifact identity. There is no second copy of language or snapshot settings. At this boundary, language and version must be explicit. The version remains an adapter-defined string; the generic contract neither chooses a default JDK nor claims support for a particular release.

`Input.Content` contains the exact original UTF-8 bytes. Paths are metadata, never instructions to open files. Preserve BOMs, CRLF, and written escapes; all emitted spans refer to the original bytes. Empty and nil content are valid empty files when size and SHA-256 match. Discovery/transcoding, if needed, happens before constructing this request and must establish the corresponding source identity.

`Input.Options.Settings` contains adapter-defined string settings; unsupported keys/values must be rejected and effective settings must contribute to `Producer.ConfigDigest`. `Input.Options.EnablePreview` is opt-in. Adapters reject unsupported language/version/option combinations with `ErrUnsupportedConfig`. They must not enable preview syntax or change versions implicitly. Effective options must contribute to the output producer's `ConfigDigest`, so serialized results retain configuration provenance.

The caller keeps content unchanged during `Parse`. The adapter does not mutate it or retain mutable references after returning. Returned IR owns its data and must not alias caller buffers or engine trees. Implementations are safe for concurrent calls; pooling or serializing a native engine is an adapter responsibility. Identical completed parses under identical producer/configuration versions produce deterministic IDs, ordering, diagnostics, and coverage.

## Validation and budgets

`Input.Validate()` checks explicit positive limits, source-byte size, shared `ir.Source.Validate()` metadata rules, explicit language version, exact content length, UTF-8, and SHA-256. It does not rewrite metadata. Digest hex casing is accepted without normalization. To inspect source errors, declare `var detail ir.ValidationError` and call `errors.As(err, &detail)`.

Use `DefaultLimits()` explicitly or supply every positive field. Zero never means unlimited. Initial defaults are:

| Budget | Default |
| --- | ---: |
| Original source bytes | 4 MiB |
| Engine syntax nodes, including unnamed/recovery nodes | 250,000 |
| Syntax/traversal depth, with root at depth 1 | 256 |
| Emitted IR records | 100,000 |
| Diagnostics plus coverage issues | 100 |
| Serialized complete IR bytes | 32 MiB |

IR records mean the combined lengths of `Scopes`, `Declarations`, `Imports`, `Types`, `Expressions`, `Calls`, `CallableReferences`, `References`, `TypeUses`, `Annotations`, `Statements`, and `Patterns`, plus one when `Package` is present, one when `Module` is present, and one per module directive. Nested detail objects are covered by syntax/depth and serialized-byte budgets. Output size means `encoding/json.Marshal` of the entire `ir.SourceFile`, including metadata and diagnostics.

Input validation enforces only preflight limits. Adapters check syntax budgets before IR extraction, count output records during extraction, account serialized records before retaining them, and check the final complete envelope. They reject unsupported budget configurations. Tree-sitter does not expose node/depth counters during native parsing: the Java adapter checks these limits with a bounded iterative scan of the completed tree, before query execution and extraction. Source bytes are checked before native parsing, and context cancellation interrupts native parsing. Record serialization and the final envelope check may allocate temporary JSON buffers. These fields do not bound native allocation, total process memory, or aggregate concurrency; hard native-memory isolation requires a separate worker/process policy. Hard-limit exhaustion returns `ErrLimitExceeded` and no artifact; it never silently drops rows or diagnostics to fit a budget.

Callers supply deadlines through `context.Context`. Adapters check cancellation around preflight and during engine/extraction work, returning identifiable `context.Canceled` or `context.DeadlineExceeded`. Engine cancellation support or process isolation must be addressed by the concrete implementation; this interface does not start background workers.

## Output and errors

| Outcome | Contract |
| --- | --- |
| Successful extraction | Nil error; structurally valid IR, exact input source identity, producer/configuration provenance, and declared coverage feature set. |
| Recoverable syntax errors or unsupported constructs | Nil error with valid IR, diagnostics, and explicit partial/failed coverage. Even failed coverage must retain a valid file/root scope and explain the loss. |
| Invalid input | `ErrInvalidInput` and zero-value IR. |
| Unsupported language/version/options or unsupported budget configuration | `ErrUnsupportedConfig` and zero-value IR. |
| Exceeded hard limit | `ErrLimitExceeded` and zero-value IR. |
| Cancellation, deadline, or engine failure | Identifiable context error or wrapped engine error, and zero-value IR. |

A non-nil Go error never carries a usable partial artifact. Orchestration will record those failures separately. Syntax facts do not contain guessed canonical targets or graph edges. Java output also includes `LanguageValidation`: extraction completeness and language validity are separate. `not_checked` requires later compiler/build validation when correctness of the program is required; `invalid` records an identified violation. `SourceFile.Validate()` checks structural integrity; neither it nor these interface tests prove extraction completeness or Java correctness.

Tests cover exact UTF-8/BOM/CRLF/escape preservation, empty files, mismatched hashes/sizes, invalid paths/metadata, explicit versions, positive budgets, byte-limit boundaries, and inspectable source errors. The Java adapter adds real extraction fixtures, malformed-input recovery and fuzz tests, deterministic/owned output checks, worker concurrency and cancellation/reuse tests, and syntax/IR/diagnostic/serialized-output budget tests.
