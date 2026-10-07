# Hermetic build-inventory fixture

`checkout/.codegraph/build-context.json` describes main/test source sets, generated source, two artifact versions, a sources JAR, compiled output and a JDK.

The JDK has only a representative `release` file and is **not runnable**. The class directory has an eight-byte class-header fixture, not executable bytecode. The three JARs are deterministic ZIPs containing only `META-INF/MANIFEST.MF`. These test byte identity and mappings without requiring a host JDK, dependency download or compiler execution.

The directory SHA-256 pins were independently computed using the documented `ei-tree-sha256-v1` framing. Tests use those committed pins as known answers, rather than asking the implementation under test to generate its own expected values. Source files and archive content are purpose-written fixtures for this project.
