# Forge JSONata

A locally maintained Python JSONata engine. The parser, evaluator, built-in
functions, custom-function API, and regex engine hook live in this package.
There are **no runtime dependencies** and no import of `jsonata-python`, Node.js
runtime, network fetch, or Git submodule involved in using or testing it.

This is a source port of `jsonata-python` 0.7.0, with its public Python API under
`forge_jsonata`. It is not a clean-room rewrite. See [UPSTREAM.md](UPSTREAM.md)
for pinned revisions, attribution, maintenance changes, and compatibility limits.
Python 3.11 or newer is required, including full upstream ISO date/time support.

```python
from forge_jsonata import Jsonata

expression = Jsonata("$sum(example.value)")
result = expression.evaluate({"example": [{"value": 4}, {"value": 7}, {"value": 13}]})
assert result == 24

expression = Jsonata("products[price <= $limit].name")
result = expression.evaluate(
    {"products": [{"name": "Apple", "price": 1.2}, {"name": "Cherry", "price": 2.5}]},
    bindings={"limit": 1.5},
)
assert result == "Apple"
```

An app in `apps/<name>` can depend on this local package using uv:

```toml
[project]
dependencies = ["forge-jsonata"]

[tool.uv.sources]
forge-jsonata = { path = "../../packages/python/jsonata", editable = true }
```

Copy the package into the Docker build context before running `uv sync`, as for
the other shared Python packages. No application currently depends on this engine.
For code previously using upstream, change `import jsonata` to
`import forge_jsonata as jsonata`, including any submodule imports.

Custom Python functions and signatures are supported:

```python
expression = Jsonata("$double(value)")
expression.register_lambda("double", lambda value: value * 2)
assert expression.evaluate({"value": 21}) == 42
```

`Jsonata`, `Constants`, `DateTimeUtils`, `Functions`, `JException`, `Parser`,
`Signature`, `Timebox`, `Tokenizer`, and `Utils` remain available from the package.
JSONata undefined and null both return Python `None` by default. Use
`expression.set_output_convert_nulls(False)` to distinguish undefined (`None`)
from JSONata null (`Utils.NULL_VALUE`).

## Runtime limits

```python
expression = Jsonata("$sum(items.price)", timeout=1000, stack=100)
```

`timeout` is milliseconds and `stack` limits evaluator depth. Exceeding these
raises `JException` with `D1012` or `D1011`. Limits are opt-in. Evaluator timeouts
start fresh for each evaluation, including after a prior evaluation raises.
Use separate expression instances for concurrent requests; the API is mutable.
Evaluator timeouts cannot interrupt a blocking Python callback or a regex operation. The compatible
`regex_engine` hook accepts a callable taking `(pattern, RegexFlags)`; an
application can supply a linear-time engine such as RE2 when needed.

## Verification

From the repository root:

```sh
make jsonata-check
make jsonata-test-re2
```

Or from this directory:

```sh
uv sync --locked
uv run --locked pytest
uv run --locked --group re2 pytest tests/upstream/re2_engine_test.py
uv build
```

Pytest automatically discovers all 1,655 pinned conformance cases, all upstream
Python tests, and local regression tests. No generation or download step is
required. RE2 tests skip in the default dependency-free engine environment and
run with the `re2` development group. The inherited 11 compatibility overrides
are unchanged and documented in [UPSTREAM.md](UPSTREAM.md).

The initial port passed **1,722 tests on each of Python 3.11, 3.12, 3.13, and
3.14**, including the optional RE2 tests: 1,655 reference cases, 59 upstream
Python tests, and eight local runtime regressions. Lint, formatting, lockfile,
source/wheel builds, and installation into an environment without upstream or
runtime dependencies were also checked. The source archive includes the full
test suite and provenance manifest; the wheel contains the engine and licenses.
