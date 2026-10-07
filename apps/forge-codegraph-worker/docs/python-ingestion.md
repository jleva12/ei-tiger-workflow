# Python ingestion

Python is registered alongside Java and TypeScript in the worker. It uses
Tree-sitter for source extraction and a pinned Pyright process for semantic
resolution. Go owns discovery, source identities, incremental invalidation and
graph projection. The implementation does not run the ingested application.

## Enable it

The worker Docker image includes Node 22, the analyzer and its bundled typeshed
stubs. Add `"python": {}` to an existing `CODEGRAPH_LANGUAGES` object. For a
Python-only worker:

```sh
CODEGRAPH_BUILD_MODE=auto
CODEGRAPH_LANGUAGES='{"python":{"platform":"Linux","roots":["src"],"dependencies":["/opt/python-dependencies"]}}'
```

The packages each checkout declares are installed for the analysis on their
own when `uv` is on the worker's `PATH` (the Docker image has it); see
[Installed packages](#installed-packages). `dependencies` adds directories the
operator installs: mount one at that absolute path when running in Docker. It
should contain installed packages and/or stubs, as a `site-packages` directory
does; ingestion reads and fingerprints it and does not invoke pip, build
backends, Python startup hooks or executable `.pth` files. Its stubs take
precedence over installed packages. What neither provides is named by its
dotted path (see [Graph behavior](#graph-behavior)).

For a native worker, install Node 22 or later and build the pinned bridge:

```sh
make python-analyzer
make test-python
```

The build verifies the SHA-256 of Pyright's pinned source archive, bundles the
internal API behind a versioned JSONL protocol, and copies its typeshed and
license. Generated files live in `packages/python-analyzer/dist/` and are ignored
by Git. `CODEGRAPH_PYTHON_ANALYZER` or `analyzer_path` can override the bridge path.

### Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `version` | repository discovery, then `3.12` | Optional explicit override; supported profiles: 3.10–3.13 |
| `platform` | `Linux` | `Linux`, `Darwin` or `Windows` target semantics |
| `roots` | `[".", "src"]` | Ordered, checkout-relative import search roots |
| `dependencies` | `[]` | Ordered, absolute dependency/stub directories |
| `install.enabled` | on when `uv` is found | Install the packages the checkout declares; `false` turns it off, `true` fails startup without `uv` |
| `install.uv_path` | `uv` on `PATH` | The uv executable |
| `install.index_url` | uv's default (PyPI) | The package index; the checkout's own index settings are never used |
| `install.cache_dir` | `$CODEGRAPH_WORK_DIR/python-packages`, else the user cache directory | Installations and uv's download cache |
| `install.max_mib` | `4096` | An installation larger than this after pruning is not used; range 64–65536 |
| `install.timeout_seconds` | `900` | Per installation; range 1–7200 |
| `install.exclude` | machine learning stacks (torch, tensorflow, jax, triton, …) | Further package names never installed; code using them resolves to their names |
| `includes`, `excludes` | `[]` | Source discovery globs; built-in excludes also apply |
| `node_path` | `node` | Node executable |
| `analyzer_path` | auto-detected | Built `bridge.cjs` path |
| `max_heap_mib` | `4096` | Analyzer heap limit; range 128–16384. Pyright keeps one whole program in memory, so size it to the context: 1,932 files of google/adk-python peak near 3 GiB and fail at 1 GiB; 733 files fit in 2 GiB. Exhaustion fails the context with an explicit `ran out of heap` error naming the file count. |
| `timeout_seconds` | `300` | Per-context resolution timeout; range 1–3600 |

In auto discovery, root `pyproject.toml` (`tool.pyright`) and `pyrightconfig.json`
provide `pythonVersion`, `pythonPlatform` and `extraPaths`; explicit worker
settings take precedence, subject to `requires-python` compatibility below.
Project metadata and common lockfiles are hashed.
The dependency contents, analyzer, target settings and ordered search paths are
included in `SourceSet.LanguageOptions["python.environment"]`, which feeds the
existing context digest. Changes invalidate the context even without Python
source edits. Adding a source file also reanalyzes its context, including calls
that previously referenced a missing import.

`syntax` mode uses explicit source settings and does not discover project
metadata or dependency directories. For custom manifests, provide a fingerprinted
`python.environment` option on each Python source set. `auto` is recommended.

### Python version discovery

Leave `version` out of the worker configuration to discover the repository's
target. Auto discovery reads root metadata without launching Python:

1. An explicit worker `version` chooses the analysis target.
2. Otherwise use `.python-version` (one numeric `major.minor` or
   `major.minor.patch` pin) and Pyright's `pythonVersion`. If both are present,
   their major/minor versions must agree; conflicting pins fail with their
   filenames and values. `pyrightconfig.json` replaces `[tool.pyright]` settings.
3. Check the target against `[project].requires-python` in `pyproject.toml`.
   An explicit worker override must satisfy this requirement too.
4. With a requirement range but no pin, retain 3.12 if compatible; otherwise
   choose the lowest supported compatible minor. This is a deterministic
   compatibility choice, not discovery of an exact installed interpreter.
5. With no version metadata, retain 3.12 and record that it is a default.

The inventory records `version_source`, `version_request` (including a patch
pin) and `requires_python` alongside the effective major/minor `version`.
These values participate in incremental invalidation.

Examples:

| Metadata | Result |
| --- | --- |
| `.python-version`: `3.11.9` | Analyze as 3.11; retain the patch pin as evidence |
| `requires-python = ">=3.13"` | Analyze as 3.13 |
| `requires-python = ">=3.10"`, no pin | Compatible default 3.12, explicitly labeled |
| `.python-version`: `3.14` or `requires-python = ">=3.14"` | Reject; no silent downgrade |
| Worker 3.12 with `requires-python = ">=3.13"` | Reject the incompatible override |

Supported requirement syntax is stable numeric release comparisons from
[Python's version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/):
`>=`, `>`, `<=`, `<`, `==`, `!=`, `~=`, comma conjunctions, and `.*` prefixes
with equality/exclusion. Minor-only profiles are compatible when some stable
patch release satisfies the range; an exact patch pin must itself satisfy it.
Prerelease, development, arbitrary-equality and nonnumeric forms are rejected
explicitly. Poetry-specific constraints, `setup.py` execution, nested project
version files, and host-dependent requests such as `system` are not interpreted.
Multiple runtimes in `.python-version` require an explicit worker target.

Discovery does not extend parser support: 3.14 remains unsupported until its
syntax and semantic behavior have been validated.

## Monorepos

A checkout of several Python projects (`apps/api`, `apps/worker`,
`packages/common`, each with its own `pyproject.toml`, `setup.cfg` or
`setup.py`) is analysed the way each project runs: its files resolve imports
from the project's directory and package directories first (`src`, Poetry
`packages[].from`, setuptools `package-dir` or `packages.find.where`, Hatch
wheel packages, PDM `package-dir`, maturin `python-source`), then from the
checkout's roots, then from the other projects, as path dependencies
installed in development mode are found. Two services that both have an
`app` or `tests` package each get their own. Each project is a Pyright
execution environment; a file belongs to the innermost project holding it.

Projects under `test`, `tests`, `fixtures`, `examples`, `docs` and similar
directories resolve within themselves but are not importable from the rest of
the checkout, so a sample project named like a real package cannot shadow
it. Hidden, excluded and installed trees (`node_modules`, `.venv`,
`site-packages`) are not searched. At most 256 projects get an environment;
the files of the rest resolve with the checkout's roots.

A repository whose Pyright configuration declares `executionEnvironments`
gets exactly those instead: each `root` resolves from itself and then its
`extraPaths`; the `extraPaths` of an environment with root `.` join the
checkout's roots. Manifests that place packages are fingerprinted, so a
layout change re-resolves the context.

## Installed packages

With `uv` available, the build-context step installs the third-party packages
the checkout's projects declare, so Pyright follows code into them
(`self.db.execute(...)` binds to SQLAlchemy's `AsyncSession.execute` instead
of an unknown). It is the Python counterpart of the Maven dependency
resolution Java ingestion does:

- **What:** `[project].dependencies` and `optional-dependencies`, PEP 735
  `[dependency-groups]`, Poetry dependencies and groups, uv and PDM
  development dependencies, `requirements*.txt` (following `-r` inside the
  checkout), `setup.cfg` `install_requires`/`extras_require`, a literal
  `install_requires` list in `setup.py` (read, never run) and `Pipfile`, of
  the root project and every shared project. `uv.lock`, `poetry.lock`,
  `pdm.lock` and `Pipfile.lock` pin versions as constraints; when the pins do
  not install together, the newest compatible versions are installed.
- **Never code:** only wheels are installed (`--only-binary :all:`), so no
  `setup.py` or build backend runs; uv runs with `--no-config` outside the
  checkout, and only the operator's index is used. Requirements that are a
  URL, a VCS reference or a path are not installed, and the checkout's own
  projects (by name or path dependency) are analysed from source.
- **Only what Pyright reads:** the installation is pruned to `.py`, `.pyi`,
  `py.typed`, top-level `.pth` files and distribution metadata, then
  fingerprinted like a `dependencies` directory. Excluded stacks are left out
  of the resolution entirely.
- **Cached:** installations are keyed by the requirements, pins, Python
  version, platform, index and exclusions, and reused until those change.
  An installation is never changed once finished; one repeated after a
  transient failure is a new directory, and unused ones are removed after 30
  days. uv's download cache beside them is emptied when it grows past four
  times `max_mib`.
- **Degrades:** a package the index does not have, or has no wheel for, is
  installed without and named in a gap (the others still install); an index
  that cannot be reached or a timeout leaves the context with the last
  installation of those requirements (or the operator's directories and
  names), with a gap, and is tried again six hours later rather than on
  every run; an installation over `max_mib` is not used, with a gap.

The Python version and platform the analysis targets choose the wheels
(`x86_64-manylinux_2_28` for Linux). uv needs some Python interpreter on the
worker to install for another; any version will do.

## Graph behavior

- Extracts `.py` and `.pyi` declarations, imports, decorators, annotations,
  invocation expressions, keyword and expanded arguments, comprehensions,
  lambdas, context managers and pattern captures, with UTF-8 byte spans.
- Analyzes one coherent program per source set, including unchanged context
  files, cyclic imports, aliases, inherited methods, inferred factory results,
  receiver narrowing, callable objects and `super()`.
- Emits `calls`, `references`, `uses_type`, `inherits` and `overrides` through
  the shared matcher/projector. Python bindings use `provenance=type_analyzer`.
- Maps source modules to file entities, source-defined instance attributes to
  derived members, and external targets to fingerprinted artifact identities.
  Separate stub overload declarations have distinct identities, but a use
  binds to one of them: the `@overload` stubs of a function and its
  implementation, or a name defined again in another branch, are one runtime
  entity, and the use binds to the implementation or the last definition.
- A call of a function whose every definition is decorated binds to the
  function, whatever callable the decorator returns (`@asynccontextmanager`,
  `@lru_cache`, a `ParamSpec` wrapper); `@overload` keeps overload resolution.
- One variable is one target where Pyright lists all its assignments because
  code flow cannot say which reaches a name (a closure's captured variable, a
  parameter assigned again, a `nonlocal` or `global` one): its first
  declaration. A class attribute also assigned through `self` binds to the
  class's declaration; the same member of different classes stays ambiguous.
- Implicit names bind to typeshed: a module's `__name__` and `__file__` to
  `types.ModuleType`'s attributes, a class body's `__doc__` and `__module__`
  to `object`'s, `__qualname__` to `type`'s.
- `Literal[...]`'s arguments and `Annotated[T, ...]`'s metadata are values: a
  string there is no forward reference, and `Annotated[int, Depends(get_db)]`
  calls `Depends` and references `get_db`.
- Preserves ambiguous binding alternatives on `unresolved_reference` nodes as
  `candidate_target_ids`, `candidate_role=binding_alternative` and
  `has_unknown_candidates`. Those IDs are persistent graph entities. Candidate
  evidence does not emit definite `calls` edges. Java's broader overload search
  candidates are unaffected.
- Missing targets, `Any`/unknown alternatives, analyzer diagnostics and source
  mapping failures remain explicit. The bridge retains same-signature callable
  alternatives that Pyright's type union would otherwise coalesce.
- What a package that is not installed provides is named by its dotted path
  from the module (Pyright can resolve nothing in it): `numpy.array`,
  `fastapi.FastAPI().get` for `app.get` when `app = FastAPI()`, and
  `beanie.Document.find_one` for a member a class inherits from such a base.
  These are external symbols of the artifact `python:<top-level module>`,
  pinned to no version. A module the repository has itself (a module file or
  regular package under a root or project, or a namespace directory holding
  what is imported from it) is never named this way: an import of it that
  does not resolve is a configuration problem and stays unresolved. A
  directory of scripts named like a package, such as alembic migrations, does
  not hide the package.
- A diagnostic does not unbind a site that has a single target: an `Optional`
  concatenated or an argument of the wrong type around a name leaves the name
  referring to what it refers to. A diagnostic is the reason, and
  `source_diagnostic` the cause, of a site that binds nothing.

Class invocations target the class's static construction contract. Method edges
identify the declaration selected by static analysis; they do not certify the
runtime implementation under monkey-patching or arbitrary dynamic dispatch.

## Boundaries

- One Pyright program per checkout, with an execution environment per
  project (see [Monorepos](#monorepos)). Per-environment `pythonVersion` and
  `pythonPlatform` are not applied; the checkout's target is.
- `pyrightconfig.json` is read the way Pyright reads it, with comments and
  trailing commas. Only the resolution settings listed above are consumed.
  Virtual environments, custom `stubPath`, editable-install hooks and ambient
  `sys.path` are not discovered; provide their source/stub paths explicitly.
- Complex or escaped string annotations and generic parameter bounds/defaults
  or generic type-alias parameters have explicit partial extraction coverage.
  Tree-sitter recovery is retained; syntax or structural-limit failures cannot
  masquerade as complete extraction.
- Computed callable collections and unknown callable contracts can retain
  candidates while remaining unresolved, as do calls through a value typed by
  a `Callable` alias (an ASGI app's `self.app(scope, receive, send)`), which
  has no declaration to bind to. Framework-specific dependency injection
  (pytest fixtures, FastAPI `Depends`), route registration and runtime tracing
  are not implemented: an unannotated parameter a framework fills has no type.
  Tests are not analysed by default (see the worker README), which leaves
  pytest fixtures out.
- External provenance is per dependency root, not per installed distribution.
  Source/dependency inventories are static and do not certify an executable build.
- The analyzer is bounded by heap, timeout and output limits. Go retains each
  context's semantic results until input fingerprints are verified; very large
  contexts still require corresponding worker memory.
- Shared IR is now version 1.3.0. The parser cache will rebuild old IR entries.

## Validation

`make test-python` runs both analyzer protocol tests and Go parser, environment,
configuration and resolver tests. Tests cover ambiguous unions, callable origin
identity, narrowing, source/stub targets, empty modules, overrides, unchanged
sources, negative imports, Unicode positions, diagnostics, budgets and input
fingerprint changes. Resolver fixtures also run the real matcher and projector.

To exercise a checkout through discovery, parsing, resolution, matching and
projection without importing it:

```sh
CODEGRAPH_TEST_PYTHON_CHECKOUT=/absolute/path/to/checkout \
CODEGRAPH_TEST_PYTHON_INCLUDE='src/**' \
go test ./apps/forge-codegraph-worker/internal/resolve/python -run '^TestResolveCheckout$' -v
```

Set `CODEGRAPH_TEST_PYTHON_DEPENDENCIES` to a platform path-list of dependency
directories for that test. This test uses an in-memory workspace; it does not
publish a graph to Spanner. See the ADK validation report (in the ei-aitiger-codegraph repository)
for measured results and their scope.
