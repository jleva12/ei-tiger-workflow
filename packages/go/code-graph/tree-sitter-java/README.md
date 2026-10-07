# Pinned Java grammar patch

EI grammar version: `0.23.5-ei.1`. This is a vendored subset of [tree-sitter-java v0.23.5](https://github.com/tree-sitter/tree-sitter-java/tree/v0.23.5), under its retained [MIT license](LICENSE). The original grammar, configuration and Go binding came from the Go module cache for that exact version. The backend module replaces only that grammar dependency; the Go Tree-sitter binding remains `v0.25.0`.

Upstream `grammar.js` SHA-256: `b89ada7566bda16a8d7a710755c27824d6097e44b8d3b1dafbb749f3c34d13e0`.

The local changes are deliberately explicit:

- E04: annotations precede the varargs ellipsis; the parameter type has a named field. Later alternatives in a multi-catch permit annotated types.
- E03: type/record patterns have named type/name/body fields, pattern-variable modifiers, qualified record types and ordered nested components. The added type/invocation conflict preserves the alternative grammar paths.
- E02/E03: `case null, default` has an explicit label production, preserving the default keyword instead of extracting a value reference named `default`.
- E07: every service provider, including the first, has a `provider` field.

Generated `src/parser.c`, `src/grammar.json`, `src/node-types.json` and headers are included so ordinary builds need only Go and a C compiler. `tree-sitter.json` retains upstream metadata; other language bindings and editor queries named there are not part of this subset. The application's extraction queries live in `apps/forge-codegraph-worker/internal/parser/java/queries`.

Regenerate with Tree-sitter CLI **0.25.0**, ABI **14**, Node.js and Python 3 available:

```sh
# From the project root; optionally set TREE_SITTER to the CLI's absolute path.
sh packages/go/code-graph/tree-sitter-java/generate.sh          # regenerate in place
sh packages/go/code-graph/tree-sitter-java/generate.sh --check  # compare against the committed sources
go test ./apps/forge-codegraph-worker/internal/parser/java
```

The check regenerates in a temporary directory and compares all generated sources and the Go binding's parser hash. The generation script updates that hash because cgo's build cache does not track included C files outside the binding directory. Always use the script after editing the grammar. The retained upstream unnecessary-conflict warning is harmless; differences or a generation error fail the check. See the [Tree-sitter generation documentation](https://tree-sitter.github.io/tree-sitter/cli/generate.html).

Java-valid fixtures and expected/forbidden extraction assertions live in the [acceptance suite](../../../../apps/forge-codegraph-worker/internal/parser/java/requirements_test.go), [flow/integrity suite](../../../../apps/forge-codegraph-worker/internal/parser/java/requirements_integrity_test.go), and optional [compiler suite](../../../../apps/forge-codegraph-worker/internal/parser/java/javac_test.go). Future grammar updates require regeneration, those tests, malformed-input checks and a new grammar/producer version.
