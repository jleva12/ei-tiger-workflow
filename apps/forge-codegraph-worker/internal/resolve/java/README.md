# Java semantic resolution

This package is the javac-backed `semantic.Resolver`. Javac is the only binding
authority: there is no source-only fallback resolver. Generic ingestion code
supplies the `semantic.Workspace`; it has no Java imports, compiler flags, or
type rules.

`New(Config{JavaHome, WorkDir, CacheDir, MaxHeapMiB, Parallelism})` validates
the service-owned JDK (`bin/java`, `bin/javac`). `Resolve` attributes the
requested compilation contexts (`ResolveRequest.Contexts`, or every source set
of the build inventory) with exactly one `java` process per call:

1. Every file of each requested source set is materialized from the workspace
   into `<WorkDir>/javac-*/sources/<setDigest>/<path>`, so javac sees exactly
   the discovered variants and nothing else on its source path.
2. Classpath entries (pinned JDK, JARs, reactor class directories) are
   verified against the inventory's fingerprints, at most once per call and
   cached in memory by (path, size, mtime) across calls.
3. Contexts are ordered so a set is attributed before every set that has its
   compiled output on the classpath. The bridge (`compiler/BindingBridge.java`,
   compiled once per source digest into `<CacheDir>/bridge/<sha256>/`) runs a
   fresh `JavacTask.analyze()` per context in one JVM, `Parallelism` contexts
   at a time, writes each context's events as JSONL to its own file and prints
   a `context_done` line on stdout. A classpath JAR the JDK's zip file system
   refuses (newer JDKs reject zip64 fields some old JARs carry) would make
   javac fail the whole context, so the bridge leaves it out and lists it in
   `context_done` (`skipped_classpath`), which Go logs as a warning; lookups
   into it stay unresolved. Go consumes finished contexts in job order
   while the JVM works on the rest.
4. Each context's JSONL is streamed one compilation unit at a time: the events
   of a file are accumulated until the path changes, the file is resolved, and
   the events are discarded. Nothing is spooled to SQLite and no context's
   events are held in memory.

Lombok is the one annotation processor the bridge runs. A source set with
`org.projectlombok:lombok` on its classpath is attributed with that JAR as its
only `-processorpath`; when it cannot run on the service JDK (Lombok supports
each JDK from some release on; 1.18.32 fails on JDK 24), the bridge tries
`Config.LombokJAR`, then none, and `context_done` lists what failed
(`processor_failures`, logged; with no Lombok running, the run gets a
warning). The bridge records every tree of a unit when javac has entered it,
before Lombok runs: code Lombok adds is never reported, and a target whose
tree is not in that set and whose origin is `EXPLICIT` (javac's default
constructor is `MANDATED`) is marked `generated` with its nearest source type
(`source_owner*`). Such targets are `lombok_generated` derived symbols owned by
that type, or by a type Lombok generated inside it (a builder), itself
derived; the source type is the contributor. Members a Lombok set's bytecode
has and its sources lack map to the same symbols from other sets; a
no-argument constructor there stays an `implicit_constructor`.

A source set the build left without compiled output (a `compiled_output`
gap) that a selected set sees, directly or through another such set, is
compiled first, one at a time in dependency order, with `generate` naming a
scratch directory: after its events are written (code generation lowers the
trees in place), the bridge writes its class files there if it has no errors
(`classes_written` in `context_done`). The directory is registered as that
set's output before anything is attributed, so its declarations are indexed
and its dependents' bytecode maps back to them; the result lists the set in
`CompiledOutputs`, and ingestion drops its gap from the run's warning. A set
not selected is compiled for its class files only (`generateOnly`): its
events are not read, and its keys come from the previous generation. A set
javac rejects gets no directory, and lookups into it stay unresolved.

Reading an element can make javac attribute a tree it left alone, which
reports errors for the unit being walked; with a processor it leaves more
(an erroneous generic call's arguments). The bridge holds a unit's events
until its walk ends and writes its diagnostics, late ones included, first, so
every file's events stay one group; an error reported for a unit already
written is left out.

Per file the resolver writes source symbols for every declaration (affected
or not; keys come from javac's attributed signatures, never parser text) in
batches of 1000, and for affected files one `PutLookups` call with call, type,
member, inheritance, override, implements and framework lookups. Targets are
mapped back to `(FileID, DeclarationID)` through the target file's parsed IR
(affected files, LRU of 8) or its previous-generation identity map (unchanged
files). External JDK/JAR entities are external symbols with the build input's
fingerprint; primitives are intrinsics; arrays, wildcards, unions and
intersections are constructed symbols with component symbols; implicit
constructors, enum/record built-ins and compact record parameters are derived
symbols tied to their source owners. Reactor bytecode maps back to the
producing set's declarations by compiler signature.

`override` lookups bind a method declaration to every method javac reports it
overrides (all supertypes, `Elements.overrides`). `implements` lookups bind a
lambda or method reference occurrence to the single abstract method of its
attributed functional interface, plus a type lookup for that interface on the
same occurrence. Unresolved, ambiguous and unsupported lookups keep their
cause, reason and compiler diagnostic code; they never fail the run.

Validation:

```
go test ./apps/forge-codegraph-worker/internal/resolve/java
CODEGRAPH_TEST_JAVA_HOME=/absolute/pinned/jdk go test ./apps/forge-codegraph-worker/internal/resolve/java
CODEGRAPH_TEST_JAVA_HOME=/absolute/pinned/jdk CODEGRAPH_TEST_LOMBOK_JAR=/abs/lombok-1.18.38.jar \
  CODEGRAPH_TEST_OLD_LOMBOK_JAR=/abs/lombok-1.18.32.jar go test ./apps/forge-codegraph-worker/internal/resolve/java
```

The Lombok tests need a Lombok JAR that runs on the test JDK; the fallback
test also needs one that does not.

The integration tests need a JDK directory without symlinks (its tree digest
pins the toolchain). They exercise real attribution across two files, override
and implements binding, diagnostic rejection, reactor bytecode mapping,
scratch cleanup and the bridge cache.

**Errors at a site.** javac often attributes a target at a place it also
reports an error: a private member used from outside, a method used from a
static context, a value that does not convert. The code refers to that
declaration all the same, so the binding stands; only errors that make
javac's choice unreliable (`compiler.err.ref.ambiguous`,
`compiler.err.cant.apply.symbols`) turn it unresolved. A call's own errors
are those before its first argument: an unknown variable passed along is
the argument's error, not the call's. When an argument is erroneous javac
still chooses a method, arbitrarily among overloads, so the bridge binds
such a call only when no other accessible method of its name takes that
many arguments. A lambda's functional interface comes from its context, so
an error in its body leaves that binding in place.

**Compiling past a broken file.** A source set the build left without
classes is compiled by the bridge for the sets that need it. javac writes no
class files for a compilation with any error, so when the set has errors the
bridge compiles it again without the files javac reported them in, then
without the files that fail once those are gone, up to six passes, and
writes the classes of what remains. One broken file no longer takes a whole
module out of its dependents' classpath; the run names the files left out,
and references into them stay unresolved.

