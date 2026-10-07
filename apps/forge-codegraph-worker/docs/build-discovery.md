# Declaration-only build discovery

`CODEGRAPH_BUILD_MODE=auto`, `maven` and `gradle` select the
[`internal/buildcontext/discover`](../internal/buildcontext/discover) provider.
It reads Maven and Gradle declarations from the checkout and returns a sealed,
validated build context without running Maven, Gradle, wrappers, plugins,
annotation processors or any repository code, and without touching local
dependency caches or JDK installations.

The context it returns is always **incomplete**: dependency coordinates are
retained as unavailable artifacts, source sets keep missing-environment
classpath entries, and JDK metadata is unknown. The javac resolver requires a
pinned JDK and a verified classpath for every source set, so a run in one of
these modes fails at the resolve stage. Use this provider to inspect how a
repository is laid out; use [`maven-resolved`](java-maven-inputs.md) to ingest
it.

## Selection

1. An existing `.codegraph/build-context.json` wins and goes through the
   validated manifest provider. An invalid or symlinked manifest is an error,
   never a silent fallback. A custom `CODEGRAPH_MANIFEST` path or a
   `CODEGRAPH_ROOTS` mount requires the manifest to exist.
2. Otherwise a root Maven or Gradle build is inspected. When both exist at the
   same root, `CODEGRAPH_BUILD_MODE=maven` or `gradle` must choose; forced
   modes bypass manifests.
3. Without a root build, independent nested Maven or Gradle builds are
   discovered, including mixed monorepos. Build files under `src`, `target`,
   `build` or `testdata` are not independent build candidates; explicit
   reactor and settings references still work.
4. If no Java compilation source set can be discovered, one explicitly
   unconfigured repository-wide extraction context is created and reported.

Java files outside the selected source roots are reported in a diagnostic
with a directory count and bounded examples; they are never assigned to
another compilation environment. Paths and IDs never contain the checkout's
absolute location, so the context ID is the same on every machine.

## Supported declarations

| Build system | Discovery |
| --- | --- |
| Maven | `pom.xml`, reactor modules, local parent chains with coordinate checks, inherited properties, module coordinates, source and test directories, compiler release and source properties and plugin configuration, default compile and test compiler executions, literal build-helper roots, inherited dependency declarations and local dependency-management versions. |
| Gradle | Groovy and Kotlin build and settings files, literal includes and nested project paths, literal `projectDir` remapping, unconditional `allprojects`/`subprojects` and explicit project blocks, Java toolchain and source compatibility, common `JavaCompile` release settings, main and test roots, named and created custom source sets, literal `srcDir`/`srcDirs`/`setSrcDirs`, simple `gradle.properties` interpolation, and literal dependency coordinates. |

The Gradle reader tokenizes comments, strings, delimiters and blocks before
inspecting declarations; text inside comments, strings, functions or
conditional blocks is never an unconditional setting. It is a bounded
declaration reader, not a Groovy or Kotlin interpreter. Main and test code get
separate source-set identities.

Java release precedence is explicit compiler release, then source language
level, then the Gradle toolchain. An unresolved release uses the adapter's
`fallback_release` (default 21) with a diagnostic on every affected source
set; the fallback never overrides a discovered release. The parser accepts
only 8, 11, 17 and 21.

## Known gaps

External Maven parents and BOMs, profile activation, transitive dependency
mediation, dependency bytes, exact classpath order, JPMS partitioning,
generated outputs, Gradle version catalogs, convention plugins, applied
scripts, composite builds, Android variants, custom task logic and source
include or exclude filters are not evaluated. Each recognized gap is retained
as a diagnostic or a missing-input entry rather than filled in.

## Resource and filesystem contract

The provider honors the build-context limits for configuration bytes,
directory entries, nesting depth, records, diagnostics and serialized output
(defaults: 4 MiB input, 2,000,000 entries, depth 128, 1,000,000 records,
20,000 diagnostics, 128 MiB output). It reads through `os.Root`, rejects build-file
symlinks, does not follow source symlinks, skips `.git`, `.gradle`, `.idea`,
`node_modules` and `.codegraph-work`, and refuses parent, module and source
paths that escape the checkout. Malformed XML, DTDs, duplicate core POM
fields, parent cycles, invalid UTF-8 and exceeded limits fail with explicit
errors. Calls are cancellable and safe to run concurrently.

## Verification

```sh
go test ./apps/forge-codegraph-worker/internal/buildcontext/discover
go test -race ./apps/forge-codegraph-worker/internal/buildcontext/discover
```

The expected-result tests cover Maven inheritance and dependency scope,
compiler execution boundaries, missing parents, BOMs and profiles, Groovy and
Kotlin layouts, toolchain and release precedence, custom source sets, project
remapping, ignored comments and conditional blocks, mixed monorepos, unmapped
Java files, fallback diagnostics, symlinks, cycles, resource limits,
mount-independent identities, concurrent ownership and the guarantee that no
repository code executes.

References: [Maven POM](https://maven.apache.org/pom.html),
[Maven compiler parameters](https://maven.apache.org/plugins/maven-compiler-plugin/compile-mojo.html),
[Gradle Java builds](https://docs.gradle.org/current/userguide/building_java_projects.html),
[Gradle multi-project builds](https://docs.gradle.org/current/userguide/multi_project_builds.html),
[Gradle source sets](https://docs.gradle.org/current/userguide/java_plugin.html).
