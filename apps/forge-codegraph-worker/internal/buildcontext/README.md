# internal/buildcontext

The public `pkg/buildcontext` package defines versioned JDK, module, source-set, artifact and dependency inventories plus the `Provider` interface. Parsed IR references the sealed context by ID.

`manifest.Provider` implements explicit inventories from `.codegraph/build-context.json`, with checkout identity checks, mounted input fingerprints, ordered dependency paths, missing-input diagnostics and bounded reads. It executes no builds or downloads. `discover.Provider` implements that contract for Maven/Gradle declaration discovery without executing a build; its context is always incomplete. See [declaration-only discovery](../../docs/build-discovery.md). The [resolved Maven provider](../../docs/java-maven-inputs.md) in `internal/languages/java/maven` executes Maven and is the only bundled provider whose context the javac resolver attributes in full.

See the [BuildContext contract and usage](../../docs/build-context.md), [provider](manifest/provider.go), and [complete manifest fixture](manifest/testdata/checkout/.codegraph/build-context.json).

`syntax.Provider` supplies explicitly chosen syntax profiles for repository scans without a manifest. `syntax.New(release)` retains Java defaults; `syntax.NewProfiles` accepts multiple languages without requiring JDK metadata for non-Java source sets. See [language routing](../../docs/language-routing.md). Its BuildContext is incomplete and records that actual JDK, module/source-set visibility and dependencies are unknown. All providers feed the [ingestion pipeline](../../docs/architecture.md); which of them can complete a run is summarized in [language routing](../../docs/language-routing.md).
