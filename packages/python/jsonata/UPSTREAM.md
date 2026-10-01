# Source provenance and compatibility

The implementation is an owned source port, not a wrapper around an installed
upstream library. Future changes are maintained in `src/forge_jsonata`.

| Component | Repository | Pinned revision |
| --- | --- | --- |
| Python implementation and Python tests (0.7.0) | https://github.com/rayokota/jsonata-python | `8b9ad131f21d2faded454226f8339846395ab15a` |
| JSONata conformance fixtures | https://github.com/jsonata-js/jsonata | `1c3b1f40173e1ead71569b077d10c2824f534620` |

The reference revision is the Python project's own pinned submodule revision.
`tests/conformance` contains its complete `test/test-suite` directory: datasets,
JSON cases, external expression files, and suite documentation. SHA-256 hashes
in `upstream-manifest.json` protect all 1,325 files plus the Python override file.
Collection verifies those hashes and the 1,655-case/102-group counts, so missing
fixtures cannot silently reduce coverage. The source licenses and notices ship
with the package.

## Port adaptations

- Renamed the distribution to `forge-jsonata` and imports to `forge_jsonata`;
  kept the upstream library API and algorithms. The CLI is intentionally omitted.
- Added uv packaging, a lockfile, and repository check targets.
- Retained all upstream Python test methods in `tests/upstream`; changed imports
  and resolved fixture paths relative to the tests, independent of the working
  directory. Added assertions to three upstream smoke checks that discarded their
  boolean results.
- Replaced upstream's generated Python wrappers with direct pytest
  parametrization. Every case runs through the ported upstream runner with its
  original data and expectations. Python 3.10-specific skips are unnecessary
  because this package requires Python 3.11+.
- Formatted the Python files consistently and removed unused imports. Per-file
  upstream copyright notices remain in place.
- Corrected three nested `Frame` type annotations so they resolve to the public
  `Jsonata.Frame` type.
- Fixed runtime-limit reuse: each evaluation gets fresh timer/depth state,
  including limits supplied through a binding frame. Elapsed budgets use a
  monotonic clock; `$now()` and `$millis()` still use the wall clock.
- Select and restore the current expression around evaluation, so alternating
  compiled expressions preserves the correct regex engine for `$eval` and
  nested Python evaluations restore the outer context.

## Inherited compatibility exceptions

The Python project's runner contains 11 overrides. We preserve the exact file
at `tests/upstream/test-overrides.json`; none were added to make this port pass.
Passing this compatibility suite means matching the Python project, including
these exceptions, rather than proving identical behavior to every JavaScript
reference result.

| Case | Upstream allowance |
| --- | --- |
| `function-formatInteger/formatInteger.json_43` | Allows failure for number-to-words outside the 64-bit range |
| `function-formatNumber/case014.json` | Allows failure for exponent formatting |
| `function-string/case006.json` | Alternate string result `1e+20` |
| `function-sort/case009.json` | Alternate ordering for equal comparisons |
| `function-sort/case010.json` | Alternate ordering for equal comparisons |
| `function-applications/case008.json` | Allows failure for an undefined substring argument |
| `matchers/case000.json` | Allows failure for a custom matcher |
| `regex/case022.json` | Allows failure for an empty regex match |
| `function-length/case004.json` | Alternate result `2` for a UTF-16 surrogate pair |
| `function-tomillis/case009.json` | Alternate timestamp / allows failure for permissive ISO parsing |
| `regex/case034.json` | Allows failure for a capture-group expectation |

The inherited runner accepts any exception when a case expects an error; it
does not enforce exact error codes. Local regressions check error codes for
runtime limits separately. The original async reuse tests also do not constitute
a guarantee of safe concurrent reuse of a mutable expression instance.

For updates, review source changes explicitly, preserve licensing notices, and
update the fixture snapshot and manifest together only when intentionally
changing the compatibility baseline. Keep local regressions alongside the
upstream tests, and run `make jsonata-check` and `make jsonata-test-re2`.
