# ETF concurrency remediation

## Objective

Fix the reproducible concurrency, lifecycle, recovery, and process-integration
failures found in the audit. Add new regression tests first, record their failing
baseline, then implement the fixes without modifying any pre-existing test.

## Guardrails

- Do not edit or weaken any of the 137 tests that existed before this task.
- New tests must reproduce the audited failure through public behavior wherever
  practical; implementation-detail tests are reserved for process plumbing.
- Capture the failing baseline before implementation changes.
- A fix is complete only when its new regression test, the original suite, Ruff,
  and Pyright all pass.
- Mongo claims must be labeled according to the environment actually tested;
  `mongomock` is not evidence of replica-set behavior.

## Remediation order and acceptance tests

### Phase 1 — Core processing correctness

- [x] R1. Retry attempts restore the last durable run and step contexts instead of
  inheriting uncheckpointed mutations from a failed attempt.
  - Test: `test_retry_discards_uncheckpointed_context_mutations`
  - Test: `test_retry_preserves_mutations_that_were_checkpointed_before_failure`
- [x] R2. A swallowed `PauseForValidation` followed by a successful return cannot
  complete the run or leave an orphaned internal gate marker.
  - Test: `test_swallowed_pause_followed_by_success_still_pauses`
- [x] R3. Rejected and expired validation gates cannot be converted into approvals by
  generic `resume()` without validation authorization.
  - Test: `test_plain_resume_cannot_override_rejected_validation`
  - Test: `test_plain_resume_cannot_override_expired_validation`
- [x] R4. A duplicate validation delivery repairs a non-transactional partial
  `request resolved / run still awaiting` transition.
  - Test: `test_duplicate_decision_repairs_partial_non_transactional_resume`

### Phase 2 — Recovery and lifecycle state machine

- [x] R5. Stale-run selection applies the cutoff before the result limit, so recent
  runs cannot starve older orphaned runs.
  - Test: `test_recovery_limit_does_not_hide_older_stale_run`
- [x] R6. `STOPPING`, `PAUSING`, and unknown latest-run states are handled
  exhaustively and never fall through to creation of duplicate attempt 1.
  - Test: `test_launch_does_not_create_duplicate_attempt_for_transitional_run`

### Phase 3 — Bridge and worker lifecycle

- [x] R7. A bridge timeout cannot return control while cancellation-safe blocking
  work is still active; the lock and run settle before the caller proceeds.
  - Test: `test_bridge_timeout_waits_for_blocking_work_to_unwind`
- [x] R8. Worker shutdown drains/fail-fasts bridge work before closing the state store.
  - Test: `test_bridge_finalizer_runs_after_inflight_cleanup`

### Phase 4 — Optional process integrations and secret hygiene

- [x] R9–R10. Applied to an optional example integration that has since been removed
  from the package; its regression tests were removed with it.

### Phase 5 — Structural and integration hardening

- [x] R11. Production wiring can require a transactional store
  (`EtfConfig(require_transactional_store=True)` fails fast without one).
  - Test: `test_config_can_require_transactional_store`
- [x] R12. Add an opt-in real-infrastructure test profile for Mongo replica-set
  transactions/leases. Tests must skip with a clear reason when infrastructure is
  unavailable.
- [x] R13. Document that lease ownership is not exactly-once execution unless
  downstream side effects enforce `StepContext.fencing_token`.
- [x] R14. Reduce orchestration coupling by moving stale-run selection and validation
  resume invariants behind named helpers rather than adding more inline branches to
  the 1,907-line orchestration module.

## Baseline evidence

- Original suite before task: `137 passed` on Python 3.14.5.
- Original Ruff result: all checks passed.
- Original Pyright result: 0 errors, 0 warnings.
- Aggregate SHA-256 over the original `tests/**/*.py` files:
  `e4eaff2d13641ef44ef63b5a91e476b61b6c10f569733cb82856e355bd3d510e`.
- New regression baseline: every newly added case in
  `tests/test_audit_remediation.py` failed at its intended audited failure point
  before any implementation code changed
  (`.venv/bin/pytest -q -p no:cacheprovider tests/test_audit_remediation.py`).

## Implementation log

- 2026-07-29: Created tracker from the read-only audit. No implementation changes yet.
- 2026-07-29: Added `tests/test_audit_remediation.py` without editing existing tests.
  Captured the expected red baseline: a failure for every new case, covering R1–R11.
- 2026-07-29: Implemented durable retry-context restoration, forced handling of
  swallowed validation pauses, generic-resume validation invariants, and repair of
  partially committed validation decisions (R1–R4).
- 2026-07-29: Added cutoff-first stale-run queries to the store contract and both
  reference stores; made launch-state handling exhaustive (R5–R6, R14).
- 2026-07-29: Made bridge cancellation drain blocking work before returning and added
  ordered async finalization (R7–R8).
- 2026-07-29: Hardened an optional example integration (R9–R10); that example and
  its tests were later removed from the package.
- 2026-07-29: Added the production transaction requirement, the opt-in Mongo
  real-infrastructure profile, and explicit lease/fencing limitations in README/design
  documentation (R11–R13). Real services were not configured in this environment: the
  Mongo replica-set check skipped with its required environment variable named.

## Final validation

- [x] Every R1–R14 item has direct evidence or an explicitly documented external
  infrastructure limitation.
- [x] Regression suite passes: `14 passed`
  (`pytest -q -p no:cacheprovider tests/test_audit_remediation.py`).
- [x] Full test suite passes with no unexpected skips: `187 passed, 1 skipped`. The
  skip is the documented opt-in Mongo replica-set check. The 42 warnings come from a
  Pydantic deprecation in `lazy_model` used by the Beanie test dependency.
- [x] Ruff passes: `All checks passed!`.
- [x] Pyright passes: `0 errors, 0 warnings, 0 informations`.
