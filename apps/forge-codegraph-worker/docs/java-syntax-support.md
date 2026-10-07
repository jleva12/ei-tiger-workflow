# Java syntax support boundary

What the Java Tree-sitter adapter extracts, which releases it accepts, what it
deliberately does not claim, and how that is tested. Adapter `0.2.0`,
extraction feature set `java-syntax/2`, IR schema `1.1.0`, patched grammar
`0.23.5-ei.1` on Go Tree-sitter binding `0.25.0`. Binding is the
[javac resolver's](../internal/resolve/java/README.md) job; nothing here selects
a target.

## Release profiles

The parser accepts explicit release profiles `8`, `11`, `17` and `21`; preview
mode and other profiles are rejected before parsing. When syntax exceeds the
selected profile, the file's `LanguageValidation` becomes `invalid` with a
release diagnostic; otherwise it stays `not_checked`, because syntax
extraction never certifies Java validity.

| Syntax recognized by the gate | First final Java release |
| --- | ---: |
| Module declarations, private interface methods, existing-variable try resources | 9 |
| Arrow switch rules and yield statements | 14 |
| Text blocks | 15 |
| Records, compact constructors, instanceof variable patterns | 16 |
| Sealed, non-sealed and permits | 17 |
| Switch type patterns, guards, null labels and record patterns | 21 |

Preview, string-template and unnamed-pattern syntax is reported as
unsupported with partial coverage and exact source evidence; no invocation or
declaration is invented for it. General language rules, API availability,
module readability, access checks, definite assignment, switch exhaustiveness
and type correctness are javac's verdict during resolution, not the parser's.

## Representation guarantees

- Every declaration, call, callable reference, value reference, type use,
  annotation occurrence and module directive is its own record with an exact
  half-open byte span; identical calls on one line are distinct occurrences.
- Receivers are complete expressions, so `a.next().next().save()` retains each
  inner call as the next receiver.
- Object creation, `this(...)` and `super(...)` delegation, enum constant
  creation and callable references are distinct kinds; `Client::new` keeps the
  name `new`, `Outer.super::method` keeps its super qualifier.
- Written types keep every name segment with its own generic arguments;
  `TypeSegment.ArgumentSyntax` distinguishes no argument list, explicit
  arguments and diamond.
- Annotation arguments keep their names, arrays keep every element, defaults
  use the same value union, so `path` and `produces` can never be confused.
- Statements retain return, yield and throw expressions, block-lambda bodies,
  conditions, alternatives, loop headers, try resources, catches and finally,
  labels, and switch arms with arrow or colon form, guards and null or default
  labels. Type and record patterns keep their declarations and ordered
  components. Pattern variables are a separate declaration kind anchored
  lexically; flow visibility is derived later.
- Annotated varargs and annotated multi-catch alternatives parse through the
  vendored grammar patch, and adjacent field-group Javadocs are attributed to
  every declared field.
- Unicode escapes are translated before native parsing; names and operators
  use translated text while spans, spellings and literal lexemes keep the
  original UTF-8 bytes. See [JLS 3.3](https://docs.oracle.com/javase/specs/jls/se21/html/jls-3.html#jls-3.3).

## Tests

The named requirement tests live in
[`requirements_test.go`](../internal/parser/java/requirements_test.go) and
[`requirements_integrity_test.go`](../internal/parser/java/requirements_integrity_test.go);
they assert required facts and forbid the misleading representations they
replaced. [`regression_test.go`](../internal/parser/java/regression_test.go)
parses hand-written fixtures for shadowing, repeated calls, qualified names,
receiver chains and unsupported syntax against exact expectations. The
optional compiler suite in [`javac_test.go`](../internal/parser/java/javac_test.go)
compiles the fixtures with a real JDK to prove they are valid Java.

```sh
go test ./apps/forge-codegraph-worker/internal/parser/java
go test -race ./apps/forge-codegraph-worker/internal/parser/java
go test ./apps/forge-codegraph-worker/internal/parser/java -run '^$' -fuzz FuzzParse -fuzztime=30s -parallel=4
JAVAC=/abs/jdk/bin/javac go test ./apps/forge-codegraph-worker/internal/parser/java -run TestRequirementCompilerFixtures
```

The grammar patch and its regeneration procedure are documented in
[`packages/go/code-graph/tree-sitter-java/README.md`](../../../packages/go/code-graph/tree-sitter-java/README.md).
