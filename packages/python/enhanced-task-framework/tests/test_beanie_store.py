"""BeanieStateStore unit coverage (audit T25/T6/T4/T22) over ``mongomock-motor``.

Covers the store contract pieces production depends on: CAS versioning, unique-index
translation, idempotency claim/steal/rebind/release, audit sequencing, ordering
tie-breaks, the event-loop guard, and the transaction retry helpers (against stub
sessions — mongomock has no replica-set transactions). Skips when the mongo extra or
mongomock-motor is not installed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("beanie")
mongomock_motor = pytest.importorskip("mongomock_motor")
from pymongo.errors import PyMongoError  # noqa: E402

from etf.exceptions import JobInstanceAlreadyExistsError, OptimisticLockError  # noqa: E402
from etf.model import JobInstance, JobRun, StepRun, ValidationRequest  # noqa: E402
from etf.status import BatchStatus  # noqa: E402
from etf.stores.beanie_store import BeanieStateStore, MongoLockProvider  # noqa: E402


async def make_store() -> BeanieStateStore:
    store = BeanieStateStore(
        "mongodb://mock", "etf_test",
        client=mongomock_motor.AsyncMongoMockClient(),
        use_transactions=False,   # mongomock has no replica-set transactions
        commit_retry_base_delay=0.001,
    )
    await store.initialize()
    return store


def run(run_id: str = "r1", instance_id: str = "i1", **kw) -> JobRun:
    return JobRun(id=run_id, instance_id=instance_id, job_name="job", **kw)


# --------------------------------------------------------------------------- #
# CAS + uniqueness (the two hard store guarantees)
# --------------------------------------------------------------------------- #
async def test_optimistic_cas_on_runs():
    store = await make_store()
    created = await store.create_job_run(run())
    assert created.version == 1

    created.status = BatchStatus.RUNNING
    updated = await store.update_job_run(created, expected_version=1)
    assert updated.version == 2

    with pytest.raises(OptimisticLockError):
        await store.update_job_run(updated, expected_version=1)  # stale token

    assert (await store.get_job_run("r1")).status is BatchStatus.RUNNING


async def test_instance_uniqueness_is_translated():
    store = await make_store()
    await store.create_job_instance(JobInstance(id="a", job_name="j", identity_hash="h"))
    with pytest.raises(JobInstanceAlreadyExistsError):
        await store.create_job_instance(JobInstance(id="b", job_name="j", identity_hash="h"))


# --------------------------------------------------------------------------- #
# Idempotency keys: claim, duplicate, steal-after-expiry, rebind, release
# --------------------------------------------------------------------------- #
async def test_idempotency_claim_duplicate_and_steal():
    store = await make_store()
    assert await store.register_idempotency_key("k", "run-1") is None       # claimed
    assert await store.register_idempotency_key("k", "run-2") == "run-1"    # duplicate

    # An expired reservation is stolen by the next claimant.
    assert await store.register_idempotency_key("exp", "run-1", ttl=timedelta(seconds=-1)) is None
    assert await store.register_idempotency_key("exp", "run-2") is None     # stolen
    assert await store.register_idempotency_key("exp", "run-3") == "run-2"


async def test_idempotency_rebind_and_release_respect_ownership():
    store = await make_store()
    await store.register_idempotency_key("k", "reservation-run")
    assert await store.rebind_idempotency_key("k", "someone-else", "target") is False
    assert await store.rebind_idempotency_key("k", "reservation-run", "target") is True
    assert await store.release_idempotency_key("k", "reservation-run") is False  # rebound away
    assert await store.release_idempotency_key("k", "target") is True


# --------------------------------------------------------------------------- #
# Audit sequencing (T6: no upsert on the hot path) + ordering tie-break (T22)
# --------------------------------------------------------------------------- #
async def test_audit_sequence_pre_created_with_run_and_monotonic():
    store = await make_store()
    await store.create_job_run(run())
    assert [await store.next_audit_sequence("r1") for _ in range(3)] == [1, 2, 3]
    # Pre-upgrade data without a counter doc still works (fallback upsert).
    assert await store.next_audit_sequence("legacy-run") == 1


async def test_stores_on_different_databases_in_one_process_are_isolated():
    client = mongomock_motor.AsyncMongoMockClient()
    tenant_a = BeanieStateStore("mongodb://mock", "tenant_a", client=client, use_transactions=False)
    tenant_b = BeanieStateStore("mongodb://mock", "tenant_b", client=client, use_transactions=False)
    await tenant_a.initialize()
    await tenant_b.initialize()  # binding B must not re-point A's documents

    await tenant_a.create_job_run(run("run-a"))
    await tenant_b.create_job_run(run("run-b"))

    assert [r.id for r in await tenant_a.list_runs()] == ["run-a"]
    assert [r.id for r in await tenant_b.list_runs()] == ["run-b"]
    assert (await tenant_a.get_job_run("run-a")).id == "run-a"
    with pytest.raises(KeyError):
        await tenant_a.get_job_run("run-b")
    raw_a = [d["etf_id"] async for d in client["tenant_a"]["etf_job_runs"].find()]
    raw_b = [d["etf_id"] async for d in client["tenant_b"]["etf_job_runs"].find()]
    assert (raw_a, raw_b) == (["run-a"], ["run-b"])


async def test_list_overdue_validations_skips_requests_without_a_deadline():
    store = await make_store()
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def request(request_id: str, deadline: datetime | None) -> ValidationRequest:
        return ValidationRequest(
            id=request_id, run_id="r1", instance_id="i1", step_name="s",
            reason="approve", sla_deadline=deadline,
        )

    await store.create_validation_request(request("no-deadline", None))
    await store.create_validation_request(request("later", now - timedelta(minutes=5)))
    await store.create_validation_request(request("earlier", now - timedelta(hours=1)))
    await store.create_validation_request(request("future", now + timedelta(hours=1)))

    overdue = await store.list_overdue_validations(now=now)
    assert [v.id for v in overdue] == ["earlier", "later"]
    assert [v.id for v in await store.list_overdue_validations(now=now, offset=1)] == ["later"]


async def test_find_last_step_run_ties_broken_by_insertion_order():
    store = await make_store()
    first = StepRun(id="s1", run_id="r1", instance_id="i1", step_name="s")
    second = StepRun(id="s2", run_id="r2", instance_id="i1", step_name="s")
    second.updated_at = first.updated_at  # identical timestamps (fixed clock)
    await store.create_step_run(first)
    await store.create_step_run(second)
    latest = await store.find_last_step_run("i1", "s")
    assert latest is not None and latest.id == "s2"


# --------------------------------------------------------------------------- #
# Event-loop guard (T4)
# --------------------------------------------------------------------------- #
def test_cross_loop_use_fails_fast_with_clear_message():
    store = asyncio.run(make_store())        # bound to loop 1 (now closed)
    with pytest.raises(RuntimeError, match="different event loop"):
        asyncio.run(store.get_job_run("r1"))  # ordinary CRUD on loop 2


class FakeCounterCollection:
    def __init__(self) -> None:
        self.value = 0

    async def find_one_and_update(self, *args, **kwargs):
        self.value += 1
        return {"value": self.value}


class FakeLeaseCollection:
    """One lease row with the server semantics the provider relies on.

    ``expired`` stands in for the server clock passing ``expires_at``. Like MongoDB, an
    upsert whose filter contains ``$expr`` is refused (error 224).
    """

    def __init__(self) -> None:
        self.doc = None
        self.expired = False
        self.last_pipeline = None

    async def create_index(self, *args, **kwargs):
        return "ttl"

    def _matches(self, query) -> bool:
        if self.doc is None or self.doc["_id"] != query["_id"]:
            return False
        if query.get("expires_at") == {"$exists": False}:
            return "expires_at" not in self.doc
        if "$or" in query:
            return "expires_at" not in self.doc or self.expired
        return True

    async def find_one_and_update(self, query, pipeline, upsert=False, **kwargs):
        from pymongo.errors import DuplicateKeyError, OperationFailure

        if upsert and "$expr" in str(query):
            raise OperationFailure(
                "$expr is not allowed in the query predicate for an upsert", code=224
            )
        self.last_pipeline = pipeline
        values = pipeline[0]["$set"]
        lease = {
            "owner": values["owner"],
            "lease_id": values["lease_id"],
            "fencing_token": values["fencing_token"],
            "expires_at": "server-now + ttl",
        }
        if self._matches(query):
            self.doc.update(lease)
            self.expired = False
            return dict(self.doc)
        if not upsert:
            return None
        if self.doc is not None:
            raise DuplicateKeyError("held")
        self.doc = {"_id": query["_id"], **lease}
        self.expired = False
        return dict(self.doc)

    async def update_one(self, query, update):
        matched = (
            self.doc is not None
            and self.doc["owner"] == query["owner"]
            and self.doc["lease_id"] == query["lease_id"]
            and not self.expired
        )
        return SimpleNamespace(modified_count=int(matched))

    async def delete_one(self, query):
        matched = (
            self.doc is not None
            and self.doc["owner"] == query["owner"]
            and self.doc["lease_id"] == query["lease_id"]
        )
        if matched:
            self.doc = None
        return SimpleNamespace(deleted_count=int(matched))


class FakeLockDb:
    def __init__(self) -> None:
        self.leases = FakeLeaseCollection()
        self.counters = FakeCounterCollection()

    def __getitem__(self, name):
        return self.counters if name.endswith("_fencing") else self.leases


async def test_mongo_lock_uses_server_time_and_monotonic_fencing_tokens():
    db = FakeLockDb()
    provider = MongoLockProvider(db)
    await provider.initialize()

    first = await provider.acquire("instance", "worker-1", timedelta(seconds=10))
    assert first is not None
    assert first.fencing_token == 1
    assert "$$NOW" in str(db.leases.last_pipeline)
    assert await provider.acquire(
        "instance", "worker-2", timedelta(seconds=10)
    ) is None

    await first.release()
    second = await provider.acquire("instance", "worker-2", timedelta(seconds=10))
    assert second is not None
    assert second.fencing_token == 3  # the failed contender also burns a token
    assert await second.refresh(timedelta(seconds=10)) is True


async def test_mongo_lock_steals_an_expired_lease_without_an_upsert_expr():
    db = FakeLockDb()
    provider = MongoLockProvider(db)
    await provider.initialize()

    stale = await provider.acquire("instance", "worker-1", timedelta(seconds=10))
    assert stale is not None
    db.leases.expired = True  # the server clock passed expires_at

    fresh = await provider.acquire("instance", "worker-2", timedelta(seconds=10))
    assert fresh is not None
    assert fresh.fencing_token > stale.fencing_token
    assert db.leases.doc["owner"] == "worker-2"
    assert await stale.refresh(timedelta(seconds=10)) is False
    await stale.release()  # a stale holder cannot free the new lease
    assert db.leases.doc is not None and db.leases.doc["owner"] == "worker-2"


def test_mongo_lock_provider_rejects_cross_loop_use():
    provider = MongoLockProvider(FakeLockDb())
    asyncio.run(provider.initialize())
    with pytest.raises(RuntimeError, match="different event loop"):
        asyncio.run(provider.acquire("instance", "worker", timedelta(seconds=10)))


# --------------------------------------------------------------------------- #
# Transaction retry helpers (T6) — stub sessions; mongomock has no transactions
# --------------------------------------------------------------------------- #
class FlakyCommitSession:
    def __init__(self, failures: int, label: str) -> None:
        self.failures = failures
        self.label = label
        self.commits = 0

    async def commit_transaction(self) -> None:
        self.commits += 1
        if self.commits <= self.failures:
            raise PyMongoError("commit unclear", error_labels=[self.label])


async def test_commit_retried_on_unknown_commit_result():
    store = await make_store()
    session = FlakyCommitSession(failures=2, label="UnknownTransactionCommitResult")
    await store._commit_with_retry(session)
    assert session.commits == 3


async def test_commit_not_retried_for_other_errors():
    store = await make_store()
    session = FlakyCommitSession(failures=2, label="SomethingElse")
    with pytest.raises(PyMongoError):
        await store._commit_with_retry(session)
    assert session.commits == 1


async def test_run_in_transaction_replays_on_transient_error():
    store = await make_store()
    attempts = 0

    async def flaky_body():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PyMongoError("conflict", error_labels=["TransientTransactionError"])
        return "committed"

    assert await store.run_in_transaction(flaky_body) == "committed"
    assert attempts == 3


async def test_run_in_transaction_gives_up_when_budget_exhausted():
    store = await make_store()

    async def always_transient():
        raise PyMongoError("conflict", error_labels=["TransientTransactionError"])

    with pytest.raises(PyMongoError):
        await store.run_in_transaction(always_transient, attempts=2)


# --------------------------------------------------------------------------- #
# Standalone-topology detection (T6)
# --------------------------------------------------------------------------- #
class HelloDb:
    def __init__(self, reply: dict) -> None:
        self.reply = reply

    async def command(self, name: str) -> dict:
        assert name == "hello"
        return self.reply


async def test_standalone_topology_disables_transactions():
    store = BeanieStateStore("mongodb://x", use_transactions=True)
    store._db = HelloDb({"ok": 1})  # no setName, not mongos -> standalone
    await store._detect_transaction_support()
    assert store.supports_transactions is False


@pytest.mark.parametrize("reply", [{"setName": "rs0"}, {"msg": "isdbgrid"}])
async def test_replica_set_and_mongos_keep_transactions(reply):
    store = BeanieStateStore("mongodb://x", use_transactions=True)
    store._db = HelloDb(reply)
    await store._detect_transaction_support()
    assert store.supports_transactions is True
