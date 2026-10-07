# internal/discovery

`WalkWithClassifier` streams repository-relative source identities using an explicitly supplied language classifier, such as the pipeline parser registry. It matches each file to its source-set language and version. Discovery has no built-in language or extension defaults. It streams from configured BuildContext source/generated roots, hashes source bytes, preserves module/source-set identity, collapses overlapping roots within each source set, avoids symlinks and Git metadata, and enforces entry/file/hash/depth/root budgets. `ReadSource` verifies bounded bytes against discovery before parsing.

Unless `Limits.IncludeHidden` or `Limits.IncludeTests` admit them, discovery leaves out every path with a segment that starts with a dot and test code: test source sets, `src/test` in every language, `test`/`tests`/`__tests__`/`__mocks__` directories outside Java, and pytest, Django and JavaScript/TypeScript test file names (`testDirectory`, `testFile`). Reports count them as `hidden_entries`, `test_entries` and `test_source_sets`; they are not omissions.

Reports count unregistered file languages as `unsupported_files` and registered files outside the current source-set language as `other_language_files`. Counts reflect visits across compilation contexts, so a physical path may be counted more than once.

The [ingestion pipeline](../../docs/architecture.md) keeps these identities in memory as the run inventory and derives each file lineage from them. Unavailable or external generated roots and unsupported filesystem entries are reported as omissions. I/O errors, changed inputs and hard limits abort discovery. This package performs no semantic binding and retains no repository-wide file list.
