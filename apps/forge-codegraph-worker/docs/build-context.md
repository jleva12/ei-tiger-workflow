# BuildContext contract and providers

[`pkg/buildcontext`](../../../packages/go/code-graph/domain/buildcontext/model.go) is the versioned inventory of
what a commit compiles against: JDKs, modules, source sets, artifacts, inputs
with fingerprints, and explicit gaps. The [`Provider`](../../../packages/go/code-graph/domain/buildcontext/provider.go)
interface produces it. Four providers exist: the explicit
[manifest provider](../internal/buildcontext/manifest/provider.go),
[declaration-only discovery](build-discovery.md), the syntax-profile provider,
and the [resolved Maven provider](java-maven-inputs.md), which is the only one
whose context the javac resolver can attribute in full. Context and manifest
schemas are `1.1.0`; the manifest provider also accepts `1.0.0` manifests.
Source sets carry explicit language, version and options; see
[language routing](language-routing.md).

## What the context records

| Record | Meaning |
| --- | --- |
| Context envelope | Trusted repository/snapshot identity, content-derived context ID, schema, provider provenance, completeness and diagnostics. |
| Input | Logical location and expected digest for a source root, generated root, JDK directory, JAR or class directory. An explicitly unavailable input retains its ID and reason. |
| JDK | Vendor, version, major release and reference to the fingerprinted JDK home. Target release is separate and belongs to a source set. |
| Module | Build-module ID/name, checkout-relative directory, optional JPMS name and produced artifact coordinates. |
| Source set | Module, main/test/custom kind, language/version/options and ordinary/generated roots. Java sets additionally retain JDK, target release, preview, optional classes output and ordered classpath/module path. |
| Artifact | Coordinates including version/classifier/extension, binary input, optional sources JAR and source repository/snapshot/module/source-set mapping. |
| Missing input | An unresolved build fact, such as a dependency whose version cannot be established. Requested text and reason survive without fabricating coordinates. |
| Input check | Available, missing, digest mismatch or unsupported, plus the actual observed digest when hashing succeeded. |

Input, JDK, module, source-set, artifact and gap IDs are separate typed namespaces local to this context. They are not semantic Java declaration IDs. Two versions of the same artifact stay distinct through separate artifact/input records and fingerprints. Consumers must not merge them by artifact name.

The classpath and module path are ordered, fully expanded inputs. Entries reference an artifact, another source set, or a missing-input record. They retain duplicate entries and unresolved positions. A reference to another source set exposes its source/catalog or compiled output; it does not recursively append that set's dependency arrays. A future compiler adapter must decide how to materialize source dependencies into its compilation units or output directories. No main-to-test, transitive or cross-module visibility is inferred. Cyclic source-set dependencies are rejected by this version of the contract.

## Provider boundary

```go
type Provider interface {
    Build(context.Context, Request) (BuildContext, error)
}
```

`Request` contains a generic checkout descriptor and explicit positive limits. The checkout's local path is only used for reading. Its repository and immutable snapshot IDs are trusted input from checkout orchestration; the provider does not invoke Git to rediscover them. The caller keeps the checkout and external mounted inputs unchanged until all consumers finish reading them. The provider neither owns nor deletes those files.

Implementations must be safe for concurrent calls, honor cancellation, return owned data, and produce a sealed structurally valid context. The [declaration-only provider](build-discovery.md) implements the same interface without executing a build; the [resolved Maven provider](java-maven-inputs.md) executes Maven and returns a complete inventory. No consumer needs to depend on their build-tool-specific configuration format.

| Outcome | Result |
| --- | --- |
| All listed inputs verified and no declared gaps | Nil error, `complete` context. |
| Missing/unconfigured input, wrong fingerprint, unsupported filesystem type or declared gap | Nil error, `incomplete` context with retained records and deterministic diagnostics. |
| Invalid manifest/model/configuration | `ErrInvalidInput` and zero context. |
| Unsupported manifest/context schema | `ErrUnsupportedVersion` and zero context. |
| Manifest expectation conflicts with checkout | `ErrIdentityMismatch` and zero context. |
| Hard budget exhausted | `ErrLimitExceeded` and zero context. |
| Cancellation or I/O failure | Identifiable context/wrapped I/O error and zero context. |

An incomplete context is usable evidence, not an instruction to bind against missing or mismatched bytes. A binder must inspect input checks and retain the resulting uncertainty. Missing source archives, for example, do not imply that an otherwise verified binary JAR is missing. Structural validation proves internal consistency; it does not prove Java readability, API compatibility or successful compilation.

## Explicit manifest format

The default file is `.codegraph/build-context.json`, resolved beneath the checkout. Its envelope contains `schema_version`, optional `expected_repository_id` and `expected_snapshot_id`, and the typed `inventory`. See the [complete example manifest](../internal/buildcontext/manifest/testdata/checkout/.codegraph/build-context.json).

The example is an executable test fixture: its JDK/classes inputs are deliberately miniature fingerprint fixtures, not a runnable Java installation. Real deployments must export the actual JDK and artifact pins. The test data's JARs are small valid ZIP containers; the provider does not claim to validate class-file or archive contents.

`expected_snapshot_id` is optional because a checked-in manifest cannot embed the SHA of its own future commit. When provided, it must exactly match the trusted checkout. The returned context always contains the checkout's actual supplied snapshot identity. Repository expectations are also enforced when supplied.

Locations use `{ "root": "artifacts", "path": "deps/library.jar" }`. `checkout` is the reserved root for the repository; other roots must be explicitly mapped by service configuration. Absolute paths, `..`, backslashes and non-normalized paths are rejected inside the manifest. `.` means the selected root. Ordinary source roots must use `checkout`; generated, binary and JDK inputs can use configured roots. An unknown or absent mount produces missing-input diagnostics.

The provider rejects unknown fields, duplicate keys, noncanonical field-name casing, invalid UTF-8, excess nesting and trailing JSON values. It reads only within `os.Root` boundaries and accepts regular files/directories. Symbolic links in an input path or fingerprinted tree are unsupported; link-containing manifests are rejected. Prepare materialized inputs when a cache or JDK layout contains symlinks. No Maven/Gradle plugin, compiler, annotation processor, network request or generated-source command is run.

## Fingerprints and reproducible identity

Ordinary source roots are checked for existence as directories. They are pinned by the trusted repository snapshot; discovery/parser inputs separately authenticate each source file's exact bytes. All located generated-root, class-directory, JDK and JAR inputs require lowercase SHA-256 pins. A declared unavailable input may omit its digest when the bytes are not known.

JAR pins are SHA-256 over exact file bytes. Directory pins use **ei-tree-sha256-v1**:

1. Start a SHA-256 stream with UTF-8 `ei-tree-sha256-v1` followed by a newline.
2. Walk entries in preorder, sorting each directory's names by byte order. Include the root directory with empty relative name, all files, hidden entries and empty directories.
3. A directory contributes byte `D`, the relative name's byte length as an unsigned 64-bit big-endian integer, and its UTF-8 name bytes.
4. A regular file contributes byte `F`, the same length/name framing, then the 32 raw bytes of that file's SHA-256.
5. Return the final lowercase hex digest. Relative names use `/`. Times, permissions and filesystem ownership are excluded; symlinks/special files are unsupported.

Use `manifest.Fingerprint(ctx, rootDirectory, relativePath, kind, limits)` when exporting a manifest. It shares the provider's bounded streaming implementation. The independent pins committed in the test manifest check that implementation against known answers. JDK fingerprints pin the directory bytes; vendor/version/major metadata is explicitly declared by the exporter. The provider does not execute or semantically inspect the JDK to authenticate that declaration.

`CanonicalInventory` deep-copies and sorts the unordered catalogs by their IDs. It never sorts roots or dependency paths. `Producer.InputSHA256` fingerprints the canonical typed inventory with its schema. JSON whitespace/object-key order, catalog ordering and absolute mount locations therefore do not change the context ID. Changing a dependency's order/version/digest, release, missing-input observation, provider version, repository or snapshot does change it.

`Seal` derives status/diagnostics and computes `build:<sha256>` over the canonical context with its ID empty. `BuildContext.Validate()` recomputes this identity and checks the envelope, cross-references, input roles, release/JDK consistency, cycles, observations and diagnostic consistency. This hash identifies the content; it is not a digital signature or proof of a trustworthy exporter. Treat a returned context as immutable and re-seal/re-extract when its inputs change.

## How the pipeline uses it

`ingestion.Pipeline` calls `Provider.Build(ctx, Request{Checkout, Limits})`
once per run, validates the context, and checks that its repository and
snapshot identity match the checkout. Every source set becomes a parser
profile; discovery walks the set's source and generated roots with the parser
dispatcher as classifier; the run-local index records each file's module and
source set; and the javac resolver derives each context's classpath and
sourcepath from the inventory, verifying every binary input's fingerprint
before javac sees it. The resolver mounts exactly three roots: `checkout`,
`java-jdk` (the configured JDK) and `java-cache` (the Maven repository and
copied JARs). A manifest whose inputs use other root names is valid as an
inventory but cannot be attributed.

```go
build, err := provider.Build(ctx, buildcontext.Request{
    Checkout: buildcontext.Checkout{Path: checkout.Path, RepositoryID: repositoryID, SnapshotID: checkout.CommitSHA},
    Limits:   buildcontext.DefaultLimits(),
})
```

Manifest mount names must match the manifest, such as `toolchain` and
`artifacts` in the example; the service maps them to absolute directories
through `CODEGRAPH_ROOTS`, so repository configuration cannot name arbitrary
host paths.

## Budgets and acceptance tests

| Per-call budget | Default |
| --- | ---: |
| Manifest/configuration bytes | 4 MiB |
| Catalog/check/diagnostic records and root/path references | 1,000,000 |
| Diagnostics | 20,000 |
| Filesystem inspections/entries | 2,000,000 |
| Aggregate file bytes hashed | 8 GiB |
| JSON/input-path/directory-tree depth | 128 |
| Serialized context output | 128 MiB |

Every classpath entry of every source set is a record, and every gap a
diagnostic, so a reactor of a few hundred modules needs the larger budgets;
exceeding one fails the language's build, which then falls back to its
syntax profile.

`MaxFiles` counts repeated path-component inspections as well as fingerprinted directory entries; it is not a distinct-file count. Hash bytes count actual fingerprint reads across all listed inputs, including repeated reads of the same location under different input IDs. Hashing uses a reusable 32 KiB buffer; directory-name sorting is bounded by the file-entry budget. JDK fingerprinting can be substantial: the initial provider deliberately re-verifies supplied bytes on each build. A future content-addressed cache can avoid repeated verification under an explicit immutability contract.

Ceilings are 16 MiB configuration, one million records, 100,000 diagnostics, depth 256 and 128 MiB output; limits must all be positive. JSON decode/canonicalization and serialization use bounded temporary allocations. Filesystem reads are cooperative with context cancellation between operations; these budgets do not cap aggregate worker/process memory or interrupt a kernel-blocked filesystem operation.

Tests cover:

- Independent file/tree pins and JSON round-trip validation.
- Main/test separation, ordered classpath/module path, distinct artifact versions, generated sources and source/JAR mappings.
- Stable IDs across catalog/format/mount changes; changed IDs for meaningful build inputs.
- Missing inputs, declared gaps, wrong digests and links, retained unresolved path positions and zero artifacts on fatal errors.
- Exact input/output/record/file limits, depth/hash/diagnostic budgets, cancellation during fingerprinting, reuse and concurrent calls.
- Strict malformed JSON, unknown schemas, identity conflicts, path escapes, dangling references, source-set cycles and output ownership.
- A real Java-parser handoff and a bounded configuration-decoder fuzz target.

```sh
go test ./packages/go/code-graph/domain/buildcontext ./apps/forge-codegraph-worker/internal/buildcontext/manifest
go test -race ./packages/go/code-graph/domain/buildcontext ./apps/forge-codegraph-worker/internal/buildcontext/manifest
go test ./apps/forge-codegraph-worker/internal/buildcontext/manifest -run '^$' -fuzz FuzzManifestDecode -fuzztime=30s -parallel=4
make check
```
