# Resolved Maven input preparation

`CODEGRAPH_BUILD_MODE=maven-resolved` is an explicit Java-owned build mode. It
prepares the effective compilation environment before parsing and resolving
source, and it is the only bundled mode whose context the javac resolver can
attribute in full; see [language routing](language-routing.md) for the others.

Configure the Java adapter through `CODEGRAPH_LANGUAGES`:

```json
{
  "java": {
    "fallback_release": 21,
    "java_home": "/opt/codegraph/jdk",
    "maven_executable": "/opt/maven/bin/mvn",
    "cache_dir": "/var/lib/codegraph/java-cache",
    "work_dir": "/var/lib/codegraph/java-work",
    "max_heap_mib": 1024
  }
}
```

These directories must be service-owned. `java_home` must name the same pinned,
symlink-free JDK directory used by the compiler adapter. Distribution JDKs often
contain symlinks; materialize a dereferenced service-owned copy during image
creation rather than weakening input fingerprint verification. The Maven cache
uses `cache_dir/repository`; external compiler JARs are copied to immutable
`cache_dir/objects/<sha256>.jar` paths before the context is sealed.

## Build JDKs

Maven runs with `java_home` unless `build_java_homes` names older JDKs by major
version:

```json
"build_java_homes": {"8": "/opt/jdk8", "11": "/opt/jdk11"}
```

A new JDK can't build every old project: JDK 22 and later refuse the zip64
fields some old JARs carry (`Invalid CEN header (invalid zip64 extra data field
size)`, from aspectjweaver 1.8.x for example), and a dependency's POM profile
activated by `<jdk>[11,)` can pull in artifacts that no longer resolve. After
reading the effective model with `java_home`, the provider picks the oldest
build JDK that can compile every non-`pom` module: a module's `source`/`target`
level needs that JDK version, and a `release` needs JDK 9 or later for javac's
`--release`. When a module's level can't be read, or no build JDK is older than
`java_home` and new enough, `java_home` builds as before. With a build JDK, the
effective model is read again under it (JDK-activated profiles may differ) and
every later stage runs with it; if any of them fails, the stages run again with
`java_home`, and a failure then names both attempts. Stage logs of the build
JDK carry a `jdk<N>-` prefix.

Build JDKs only run Maven. Attribution always uses `java_home`, which the
context pins as its JDK input. Build JDKs aren't fingerprinted inputs, so they
may contain symlinks; each must have `bin/java`, and its version is read from
its `release` file, or from the launcher when it has none (Corretto 8 for
macOS). Configured build JDKs are part of the build-context cache key.

## Sources javac rejects

A compile error in the project's own sources does not fail ingestion, and
neither does any other failed step. When a compiling stage (`install`, or a
test-helper stage) fails, it runs again as `<stage>-past-errors` with
`-Dmaven.compiler.failOnError=false` and `--fail-at-end`, and every later
compiling stage of the build goes straight to those flags. javac writes no
classes for a module it rejects, but the module is still packaged and
installed, so the rest of the reactor and the classpath stages run. When the
failure was not javac's (a plugin that broke, a dependency that cannot be
resolved, a parallel build that raced), the rerun and every later stage build
one module at a time (`-T 1`) with a 3 GB heap, as does any stage that runs
out of memory. Steps that still fail are kept with the project Maven names;
modules they left without classes, and modules Maven then skipped, get
`compiled_output` gaps with that step as the reason. The classpath stages run
with `--fail-at-end` too: a module whose dependencies cannot be resolved gets
a `classpath:compile` or `classpath:test` gap, reported on the run in one line
for all such sets, and the others keep theirs.

The project's own JDK builds first when it is older than the service's; a
failure there that is not javac's is tried again with the service JDK before
anything is built past it.

Every stage skips the plugins that write nothing javac reads and fail most
often where the worker runs (no network, keys, Docker daemon, Git directory
or Node): Checkstyle, Spotless, RAT, JaCoCo, Javadoc and source JARs, site,
Enforcer, GPG, Invoker, SpotBugs, PMD, Animal Sniffer, license plugins,
git-commit-id and buildnumber, Docker and Jib, Spring Boot repackage and
build-image, Quarkus build, GraalVM native, CycloneDX, OWASP dependency-check,
OSS Index, Sonar, dependency analysis, duplicate-finder, japicmp, revapi,
clirr, sortpom, formatters, Tidy, ProGuard, Assembly, delombok, Gatling and
every frontend-maven-plugin step. Source generators (protoc, OpenAPI, jOOQ,
exec and antrun steps) still run.

A Maven log that outgrows its budget never fails the build: its head is
kept, then its end, whole lines, after a marker saying how much was left out.

The `[ERROR] <file>.java:[line,col]` lines javac wrote are kept. Every source
set with an error in its roots gets no compiled output, whatever its output
directory holds, and a `compiled_output` gap (`bc.GapCompiledOutput`) whose
reason is its first five errors. Dependents still see it: its test set keeps
it on the classpath, and a module that depends on it sees its source set
instead of the empty JAR it installed. Ingestion analyses every set from
source as usual. When a set without output compiles in the resolver's javac
(often the case: the build failed only because of its environment, such as
Lombok on a newer JDK), the resolver writes its class files to scratch for
the sets that need them, and the gap is not reported. Otherwise lookups into
it stay unresolved and the run succeeds with a warning naming the set and its
errors (`Run.WarningMessage`, shown in the console). A build with compile
errors is never cached, so a later run of the same snapshot builds again, for
example after a build JDK is added.

This opt-in mode invokes the **local install lifecycle**, which executes Maven
build plugins, source generators and main-source compilation in the owned
checkout. Before installation, the provider reads Maven's effective reactor
model. If a module consumes another reactor module's `tests` JAR, its producer
and reactor prerequisites are installed with `-pl <producers> -am`,
`-Dmaven.test.skip=false` and `-DskipTests`. This creates required helper JARs
even when the local cache is empty. The full reactor install then supplies
`-Dmaven.test.skip=true` and `-DskipTests`.
Project tests never execute. If expanded dependency paths consume a reactor
`tests` classifier, the provider compiles only those helper modules with
`-pl <effective-reactor-selectors> -Dmaven.test.skip=false -DskipTests test-compile`.
This phase prepares required classes and generated test sources without reaching
the `test` phase. An empty JAR left by skipped compilation does not establish
complete helper inputs. It never invokes `deploy` or publishes packages remotely.
Separate effective-model, compile-classpath and test-classpath
observations then capture dependency ordering. Resolving test dependencies is
necessary to analyze test source; it does not execute the tests.

The Java provider consumes the effective model as bounded project records rather
than retaining Maven's complete XML document. It records main/test compilation
variants and separate annotation-only compiler executions, selected JDK/release
or source/target settings, compiler include/exclude patterns, existing and
required generated roots, main output directories, and ordered visibility.
Standard reactor artifacts map to their actual source-set output directories,
preserving source symbol ownership. Classifiers without such outputs retain their
exact binary identity; consumed reactor test helpers without populated outputs
keep the build context incomplete. External JARs retain Maven coordinates and
content fingerprints.
Tests have explicit visibility to the module's main output. When main classes
come from another language and there is no Java main source set, its actual
compiled directory is retained as a fingerprinted artifact on the test classpath.
It does not acquire invented Java source declarations. Inputs are checked
again and missing classpaths, required generated directories or main outputs
keep the context incomplete. Publication retains its strict coverage checks.
After successful preparation, declared build-helper source directories with no
generated files are materialized as empty, fingerprinted inputs inside the owned
checkout. Relative build-helper paths, including `.`, resolve from the declaring
module; equivalent absolute and relative roots are deduplicated. Read-only
observation does not repair absent directories. Annotation-only
executions retain their actual includes and exclusions (including execution-level
overrides), and do not expose nonexistent class output to other source sets.

Generic `SourceSet.IncludePatterns` and `ExcludePatterns` represent compilation
source selection. Discovery applies these relative to each source root, using
portable `**` globs, and reports intentionally excluded files separately from
omissions. Maven-specific interpretation stays inside the Java adapter.

Successful observation removes private command logs and intermediate XML.
Failed commands return a bounded diagnostic tail. The checkout lifecycle removes
build outputs with that run's checkout. Downloaded immutable compiler inputs stay
in the Java cache across runs; the run-local index and the checkout are
removed when a run ends, the cache and the configured JDK are not. The default preparation timeout is 45 minutes with explicit
bounded model, log, inventory, hash and filesystem budgets.
Maven and its forked compilers/generators run in a private process group. On
cancellation or parent exit the provider kills and drains remaining children
before returning control to checkout cleanup; it does not stop waiting merely
because the request context was cancelled.

## Where the projects are

A checkout with a `pom.xml` at its root builds that. Otherwise the top-level
projects are the directories with a `pom.xml` that no other such directory
contains: one is built with `-f`, several are listed as modules of an
aggregator POM written in the build's scratch directory, which the effective
model then leaves out. `node_modules`, `target`, `.git` and similar
directories are never searched, and neither is a module's `src` directory, so
test-fixture and archetype POMs are not projects. A `pom.xml` that does not
parse is passed over; two POMs with the same coordinates are told apart by
the effective model's build directory.

## What the observer leaves out

The observer pins what it can and records a gap for what it cannot, rather
than failing the build:

- a source or generated root outside the checkout, or with a property Maven
  left unresolved (`source_root`, `generated_root:<name>`);
- a classpath entry that is not a JAR or class directory (a `type=pom`,
  ZIP or WAR dependency: left out without a gap, javac reads nothing there),
  a JAR outside both the checkout and the isolated repository
  (`classpath_entry:<file>`, once per file name), a JAR past the build's byte
  budget, or a classpath list past the input limit;
- a Java level javac cannot compile: 6 and 7 are analysed as 8, a level newer
  than the JDK as the JDK's own, an absent level as 8, and an unreadable one
  as the JDK's own with a `compiler_level` gap.

A system-scoped JAR in the checkout is pinned from the checkout, and a
timestamped snapshot JAR keeps its directory's version.

