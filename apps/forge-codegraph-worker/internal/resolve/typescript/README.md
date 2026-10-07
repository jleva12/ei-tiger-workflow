# TypeScript resolution

This package is the `semantic.Resolver` for TypeScript and JavaScript source
sets, in two tiers. The syntax tier, described here, binds every site from
the parsed IR of the source set's own files; its bindings carry
`provenance: syntax`. It never fabricates a target: a name it cannot bind is
an unresolved lookup with an explicit cause. The compiler tier
([compiler.go](compiler.go), [docs/typescript-compiler.md](../../../docs/typescript-compiler.md))
then runs the TypeScript compiler over the same sites, each file under the
tsconfig that governs it and with the project's packages installed, and
replaces every binding it proves (`provenance: compiler`); what it cannot
bind keeps the syntax tier's binding, and a compiler that fails leaves the
syntax tier's bindings with a warning and a re-resolution on the next run.

`Resolve` selects the requested contexts (or every `typescript` source set
of the build inventory), loads each file's IR from the workspace, parses an
unchanged file again from its checkout bytes when its IR left the cache (so
a barrel's re-exports stay followable), falls back to previous identities
only when that is impossible, writes one source symbol per declaration of
every file, and binds the sites of the affected files. A file the parse
stage skipped for exceeding a parser limit (a committed bundle, typically)
has no syntax to bind: it contributes no symbols and no lookups, and the
result counts it as `skipped` rather than failing the run; the parse stage
logs each skipped file with its reason.

## Module resolution

A specifier reaches a discovered file, in this order, or is external:

| Specifier | Rule |
| --- | --- |
| `./x`, `../x` | relative to the importing file, with `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.d.ts` and `index` completion; a compiled extension in the specifier (`./x.js`) also tries the source it stands for (`./x.ts`) |
| `@/x`, `config` | the `paths` of the nearest `tsconfig`/`jsconfig` above the importing file (longest matching pattern, every target tried), then its `baseUrl` |
| `@acme/core`, `@acme/core/src/x` | a workspace package by name: its `exports`, `source`, `module`, `main` or `types` entry (an entry in its build output, `dist/index.d.ts`, standing for its source, `src/index`), then `src/index` or `index`; a subpath resolves inside the package directory, then inside its `src` |
| anything else | a package outside the repository (`external_dependency`) |

Projects and packages come from the source set's language options, written
by [project discovery](../../languages/typescript/project) and read through
`DecodeSettings`; they are part of the source set's digest, so a tsconfig
or workspace change re-resolves the set.

## Exports

An import binds to what the target module exposes under the name: its own
exported declaration, or a re-export followed through other discovered
modules with a cycle guard. `export { X as Y } from "m"`, `export * from
"m"` (which never forwards a default), `export * as ns from "m"`, and the
module-level `export { X }`, `export { X as Y }` and `export default X` of
an imported or local binding are all re-exports in the IR
(`ImportReExport`). A namespace import or namespace re-export exposes the
target module's exports as members. `const x = require("m")` and
`const { a, b: c } = require("m")` bind imports, not variables.

## Binding rules

| Site | Rule |
| --- | --- |
| Name | the innermost declaration of that name along the syntactic scope chain (members are reachable only through a receiver), then the module's import binding, then a known JavaScript, DOM or Node platform name as an intrinsic; a test-framework or UMD global (`expect`, `React`) is a value outside the repository |
| Import binding | a relative specifier (`./x`, `../x`) is resolved against the source set's discovered files with `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.d.ts` and `index` completion; a named import binds to the module-level declaration of that name, a default import to the declaration written `export default`, a namespace import to the module itself |
| Member | through the receiver's type (a call of a function with no declared return type has the type of its expression body, or of the first of its own `return` values that has one; a literal, array literal, string concatenation, arithmetic, comparison, `??`/`||` fallback or conditional has the type it spells): `this` is the enclosing class, a class or interface reference is itself, a namespace import exposes the module's top-level declarations, a variable's declared type, its `new X()` initializer, an awaited `Promise<T>` or a called function's declared return type is followed one member at a time, and the members of every base of a class, interface or object-type alias are visible (an alias over an intersection or union extends its named constituents and takes its object constituents' members; `Readonly<T>`, `Partial<T>`, `Required<T>`, `NonNullable<T>`, `Awaited<T>`, `Omit<T, K>` and `Pick<T, K>` denote `T`). Arrays type their elements (`xs[i]`, `xs.find()`, `for (const x of xs)`), the callbacks of array, set and map iteration methods type their parameters, and a destructuring binding is typed by the member or element it takes. A value from a package, framework or platform stays external through every member access, so `expect(x).toBe(y)` or `useState()[0].foo` is attributed to that dependency rather than unknown |
| Call | a function, method, constructor or class found the same way; `super(...)` is the base class constructor; `new X()` binds to `X`'s written constructor or to `X` itself |
| Type | class, interface, enum, alias, namespace and type-parameter declarations, or predefined and global types as intrinsics; qualified types are followed through namespaces |
| Inheritance | `extends` and `implements` types bind as `inheritance` lookups |
| Override | a method binds as an override of the same-named method of its resolved base class |
| Decorator | the decorator expression's name chain, bound as a member lookup |

Composite types (arrays, unions, intersections, function, structural,
literal and operator types) name no declaration; their element and component
types are bound as separate uses.

## Packages and the platform library

Nothing is installed in a checkout, so a package's declarations cannot be
read; what the code takes from one is still named. An import from a package
binds to an external symbol named by the package's canonical specifier (a
Node built-in with its `node:` prefix) and the export: `react#useState`,
`node:fs#readFileSync`. A namespace or default import is the module value
itself, so `React.useState` and a named `useState` are one symbol. Members
and calls taken from such a value extend the path, with `()` for a call's
result: `axios#create().get`. A type from a package is named the same way
(`react#FC`, `axios#AxiosResponse`), and a variable declared with one types
its members by it (`axios#AxiosInstance.get`). A member a class does not
declare binds to its package base's (`react#Component.setState`), as do
`super` calls. Test-runner globals are named under `global:test-api`
(`global:test-api#expect().toBe`), and UMD globals under their package
(`React` is `react`). Every such symbol belongs to an artifact
(`npm:<package>`, `node:<module>`, `global:<api>`) and pins no version, so
it is the same entity across upgrades. A specifier that is not a package
name (an alias such as `@/components` or `~/lib` that no tsconfig maps)
names nothing and stays unresolved.

Members and calls of the platform library bind to intrinsics named the same
way: `console.log`, `JSON.parse`, `Promise.then`, `Map.set`, `fetch().then`.
A platform namespace's call is named by the call (`JSON.parse().data`);
methods of a typed value keep its type (`xs.filter(f).map` is `Array.map`).

## Unresolved causes

| Cause | When |
| --- | --- |
| `external_dependency` | a member of a chain from a package value the tier could not follow member by member (`external_value`), a member of a class whose base is a platform type (`runtime_library`), or a name that `export *` may forward from a package (`re_exported_from_external`) |
| `analysis_limitation` | a relative, aliased or workspace module with no discovered file, an export the target module neither declares nor re-exports, a default import of a module without a syntactic default export, a re-export cycle, a member the receiver's declaration does not have, a receiver whose type is not declared or constructed in the syntax, a dynamic `import()` |
| `unsupported` status | a computed callee (`fns[0]()`), a composite type where a single declaration is expected, an import of a stylesheet, data or media file (`asset_import`) |

## Keys

Module-level and member declarations carry a `semantic.DeclarationKey` built
from the module path without its extension, the owner chain and the name,
with parameter names for callables: `web/src/api#ApiClient.get(path)` under
owner key `web/src/api#ApiClient`. Locals, parameters, type parameters and
anonymous declarations have no portable key. Keys are stable across commits
that keep a declaration in place, which is what identity continuity needs.

## Measured on a real repository

reports/typescript-excalidraw-2026-09-14 (in the ei-aitiger-codegraph repository)
ingested excalidraw/excalidraw (681 files) end to end and drove the rules
above; `checkout_test.go` replays any checkout locally
(`CODEGRAPH_TEST_TS_CHECKOUT=/path go test ./internal/resolve/typescript -run TestResolveCheckout -v`)
and prints resolution statistics by cause.

## Not done yet in the syntax tier

Members of object literals assigned to variables, destructured parameters
(`({ title }: Props)`), members of union constituents beyond the first that
declares them, conditional `exports` subpath patterns and `tsconfig` project
references are beyond this tier; the compiler tier binds them when it runs.
Tracked in [closing the breadth gap](../../../docs/gap-closure.md).
