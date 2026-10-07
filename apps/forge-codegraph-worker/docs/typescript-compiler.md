# TypeScript compiler tier

TypeScript and JavaScript are bound in two tiers. The
[syntax tier](../internal/resolve/typescript/README.md) binds every site from
the parsed IR alone. The compiler tier then runs the real TypeScript compiler
over the same sites, with each project's own `tsconfig` and the packages the
project declares installed, and replaces every binding it can prove. A
compiler-bound lookup carries `provenance: compiler`; a site the compiler
cannot bind (a receiver typed `any`, a keyword type, a module namespace)
keeps its syntax-tier binding and `provenance: syntax`.

Measured on real checkouts (share of lookups resolved in application code,
outside hidden tool folders such as `.claude/skills`):

| Checkout | Syntax tier | Compiler tier, packages installed |
| --- | --- | --- |
| cyber-shield (React, Workers) | 88.1% | 99.3% |
| gemini-cli (npm workspaces) | 95.0% | 98.9% |
| tutela-vita-application | 90.6% | 99.2% |
| oil-project-mono-repo | 91.2% | 99.0% |
| node-back-pi-coding-agent (workspace packages built to `dist`) | 82.5% | 98.7% |

What stays unresolved is mostly members of values the code itself types as
`any` (test doubles, untyped API responses).

## How it runs

The bridge in [`typescript-analyzer`](../typescript-analyzer) bundles
TypeScript 6.0.3, the last release with the in-process compiler API
(TypeScript 7 is a native binary without one), and its standard library.
Build it with `make worker-install` or
`cd typescript-analyzer && npm ci --ignore-scripts && npm run build`; the
Docker image has it at `/opt/codegraph/typescript/bridge.cjs`.

For each run the resolver writes the context's files, as the parser read
them, into a scratch directory with the checkout's `tsconfig*.json`,
`jsconfig*.json` and `package.json` files, lays the installed packages out
beside them, and hands the bridge the sites of the changed files. The bridge
analyses each file with the options of the project that governs it, as an
editor does: the nearest `tsconfig.json` or `jsconfig.json` above it that
includes it, one of the projects it references (a solution tsconfig such as
Vite's `tsconfig.app.json`/`tsconfig.node.json`), or else the nearest one.
Each project's global declaration files (`env.d.ts`, `types/*.d.ts`) are
part of its program. Parsed declaration files are shared between projects.
A workspace package whose `exports` or `types` name its build output, which
is not committed (`"./audit": "./dist/audit/index.d.ts"`), resolves to the
sources that output is built from, through its tsconfig's `outDir` and
`rootDir` or the `dist`/`build`/`lib`/`out` → `src` convention, as
TypeScript project references map outputs to sources.

| Site | Binds to |
| --- | --- |
| Call, `new` | the declaration of the signature the checker resolves; an overload signature in the checkout binds to its implementation; an implicit constructor to the class |
| Name, member | the declaration of the symbol behind the name, imports followed to what they import |
| Member of a union | one member when every constituent's declaration narrows the same member of a common base (the states of a discriminated union such as TanStack Query's results); each constituent's member, as binding alternatives, otherwise |
| Type, heritage | the type declaration (class, interface, alias, enum) |
| Decorator | the decorator's declaration |
| Override | the same-named member of the base class |
| Member of an object or type literal | a member derived from the innermost declaration around it that has a portable key, named by the path below it: a zod schema's field (`web/src/schemas#TaskRunSchema.runId`), an inline props type (`web/src/view#HandoffView(props).props.taskId`), a returned object's property (`src/repo#Repo.find(id).title`); projected as a `derived_field` node with a `derived_from` edge to that declaration |

Targets become symbols the way the syntax tier names them, so one entity has
one identity across tiers:

- a declaration in the checkout is its source symbol, matched by its name's
  bytes;
- a declaration in an installed package is an external symbol named by the
  package and the qualified name of the declaration, without the entity a
  module exports as itself (`export = React`): `react#useState`,
  `axios#Axios.get`, `@tanstack/query-core#QueryObserverBaseResult.data`. A
  declaration package names the package it describes (`@types/react` is
  `react`), and an ambient Node module its built-in (`node:fs#readFileSync`);
- a declaration of the platform library is an intrinsic named by its
  qualified name: `Console.log`, `Array.map`, `HTMLInputElement.value`.

## Installed packages

With the compiler tier on and `npm` on the worker's `PATH` (the Docker
image has it), the build-context step installs each npm project of the
checkout: a `package.json` that is not a workspace package of another, with
its workspace packages and its `package-lock.json`. A monorepo of
independent apps (`apps/web`, `apps/worker`) is several projects; a
workspace root is one.

- **Never code:** npm runs with every lifecycle script off
  (`--ignore-scripts`, for the project and every package), in a directory
  holding only the project's manifests and lockfile, so no `.npmrc`,
  `.pnpmfile.cjs` or script of the checkout is read or run.
- **Only the operator's registry:** packages come from `install.registry`
  (npm's public registry by default). A lockfile entry fetched from another
  host (a private registry the checkout's `.npmrc` names), a git, tarball or
  GitHub dependency, and a package the registry does not have are left out
  and named in a gap; the rest install from the lockfile's versions, or from
  the manifests' ranges when the lockfile no longer applies. Path
  dependencies (`file:`, `link:`, workspace packages) are the checkout's own
  code, analysed from source.
- **Only what the compiler reads:** an installation keeps each package's
  `package.json`, declarations (`.d.ts`, `.d.mts`, `.d.cts`), TypeScript
  sources and `tsconfig` bases; JavaScript, native binaries and assets are
  removed (cyber-shield's web app: 418 MB installed, 74 MB kept).
- **Cached:** installations are keyed by the manifests, lockfile, registry,
  npm version and exclusions, never changed once finished, and removed after
  30 days unused. npm's download cache beside them is emptied when it grows
  past four times `install.max_mib`.
- **Degrades:** a registry that cannot be reached or an install out of time
  leaves the project without packages, with a gap, and is tried again six
  hours later rather than on every run; an installation over
  `install.max_mib` is not used.
- **Any package manager:** npm installs pnpm, Yarn and Bun projects too. Their
  workspace packages (listed in `pnpm-workspace.yaml`, or in a `workspaces`
  object npm does not accept) are handed to npm as an explicit `workspaces`
  list; `workspace:` links are linked by name, and one to a package the
  checkout does not hold is left out rather than fetched from the registry
  under the same name; `catalog:` and `catalog:<name>` ranges take the
  version of pnpm's catalogs in `pnpm-workspace.yaml` or Bun's in the root
  `package.json`. npm reads only its own lockfile, so those projects get the
  newest versions their ranges allow rather than their locked ones; ranges
  stay within a major version, so the APIs bound rarely differ. A Yarn Plug'n'Play
  project gets a `node_modules` the compiler reads.

## Settings

In the `typescript` object of `CODEGRAPH_LANGUAGES`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `compiler.enabled` | on when `node` and the bridge are found | `false` binds by syntax alone; `true` fails startup without them |
| `compiler.node_path` | `node` on `PATH` | The Node executable |
| `compiler.analyzer_path` | `CODEGRAPH_TYPESCRIPT_ANALYZER`, the image's, or the source tree's build | The built `bridge.cjs` |
| `compiler.max_heap_mib` | `4096` | The compiler's heap; range 256–32768 |
| `compiler.timeout_seconds` | `900` | Per run; range 1–7200 |
| `install.enabled` | on with the compiler when `npm` is found | `false` leaves packages out; `true` fails startup without the compiler or npm |
| `install.npm_path` | `npm` on `PATH` | The npm executable |
| `install.registry` | `https://registry.npmjs.org/` | The only registry packages come from |
| `install.cache_dir` | `$CODEGRAPH_WORK_DIR/typescript-packages`, else the user cache directory | Installations and npm's download cache |
| `install.max_mib` | `4096` | An installation larger than this after pruning is not used; range 64–65536 |
| `install.timeout_seconds` | `900` | Per project; range 1–7200 |
| `install.exclude` | none | Package names never installed |

Both run only in auto discovery. The bridge's fingerprint and every project
file the compiler reads are part of the source set's options, and each
installation's digest too, so a tsconfig, manifest, lockfile or bridge
change re-resolves the set.

## When the compiler cannot run

A missing bridge, a compiler that crashes, runs out of its heap or out of
time leaves the sites it had not answered to the syntax tier: the run
succeeds with a warning ("The TypeScript compiler did not bind every
reference …"), and the context is resolved again on the next run. Answers
the bridge gave before stopping are kept.

## Validating

`make worker-test-typescript` builds the bridge and runs its tests and the
Go TypeScript tests with the compiler required. To measure a checkout:

```sh
CODEGRAPH_TEST_TS_CHECKOUT=/path/to/checkout \
CODEGRAPH_TEST_TS_COMPILER=1 \
CODEGRAPH_TEST_TS_INSTALL_CACHE=/tmp/ts-packages \
CODEGRAPH_TEST_TS_FILES=1 \
go test ./internal/resolve/typescript -run TestResolveCheckout -v
```

It prints resolution by cause, the files with the most unresolved lookups,
and the share resolved outside hidden tool folders.
