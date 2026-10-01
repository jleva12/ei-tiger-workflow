"""Opt-in checks against a real MongoDB replica set or mongos."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

from etf import BatchStatus, JobInstance

pytestmark = [
    pytest.mark.real_infrastructure,
    pytest.mark.timeout(120),
]


def _mongo_uri() -> str:
    uri = os.getenv("ETF_REAL_MONGO_URI")
    if not uri:
        pytest.skip(
            "set ETF_REAL_MONGO_URI to a disposable Mongo replica-set or mongos URI"
        )
    return uri


async def test_real_mongo_lock_provider_leases():
    """Lease semantics on a real server; a standalone mongod is enough."""
    pytest.importorskip("beanie")
    from motor.motor_asyncio import AsyncIOMotorClient

    from etf.stores.beanie_store import MongoLockProvider

    client = AsyncIOMotorClient(_mongo_uri(), serverSelectionTimeoutMS=5000)
    db_name = f"etf_integration_locks_{uuid.uuid4().hex}"
    try:
        locks = MongoLockProvider(client[db_name])
        await locks.initialize()
        key = f"lease-probe:{uuid.uuid4().hex}"

        first = await locks.acquire(key, "owner-a", timedelta(milliseconds=300))
        assert first is not None
        assert await locks.acquire(key, "owner-b", timedelta(seconds=5)) is None

        await asyncio.sleep(0.4)  # the server clock passes the lease's expiry
        second = await locks.acquire(key, "owner-b", timedelta(seconds=5))
        assert second is not None
        assert second.fencing_token > first.fencing_token
        assert await first.refresh(timedelta(seconds=5)) is False
        await first.release()  # a stale holder cannot free the new lease
        assert await locks.acquire(key, "owner-c", timedelta(seconds=5)) is None

        assert await second.refresh(timedelta(seconds=5)) is True
        await second.release()
        third = await locks.acquire(key, "owner-c", timedelta(seconds=5))
        assert third is not None

        contenders = await asyncio.gather(
            *(
                locks.acquire(f"{key}:race", f"owner-{i}", timedelta(seconds=5))
                for i in range(20)
            )
        )
        assert sum(lock is not None for lock in contenders) == 1
    finally:
        await client.drop_database(db_name)
        client.close()


async def test_real_mongo_lifecycle_with_the_mongo_lock():
    """Launch, gate, approve, fail, restart and re-drive through BeanieStateStore and
    MongoLockProvider, plus two tenant stores in one process; a standalone mongod is
    enough (transactions are used when the server supports them)."""
    pytest.importorskip("beanie")
    from etf import (
        Actor,
        EtfConfig,
        JobDefinition,
        JobLauncher,
        JobOperator,
        JobParameters,
        JobRegistry,
        LaunchRequest,
        Step,
        StepResult,
        ValidationDecision,
    )
    from etf.audit import StoreBackedAuditSink
    from etf.policies import BackoffRetryPolicy
    from etf.stores.beanie_store import BeanieStateStore, MongoLockProvider

    class Gate(Step):
        name = "gate"

        async def execute(self, ctx):
            await ctx.request_validation(reason="approve")
            return StepResult.completed()

    class Charge(Step):
        name = "charge"
        fail = True

        async def execute(self, ctx):
            if Charge.fail:
                raise ConnectionError("payment API unavailable")
            return StepResult.completed()

    names = [f"etf_integration_{uuid.uuid4().hex}" for _ in range(2)]
    stores = [BeanieStateStore(_mongo_uri(), db_name=name) for name in names]
    try:
        for store in stores:
            await store.initialize()
        store = stores[0]
        assert store._db is not None  # noqa: SLF001 - integration wiring probe
        locks = MongoLockProvider(store._db)  # noqa: SLF001
        await locks.initialize()
        registry = JobRegistry()
        registry.register(JobDefinition("pay", [Gate(), Charge()]))
        cfg = EtfConfig(
            store=store,
            audit=StoreBackedAuditSink(store),
            lock_provider=locks,
            registry=registry,
            retry_policy=BackoffRetryPolicy(max_attempts=1),
        )
        launcher, operator = JobLauncher(cfg), JobOperator(cfg)
        request = LaunchRequest(
            job_name="pay",
            parameters=JobParameters(identifying={"order": "A-1"}),
            idempotency_key="delivery-1",
        )

        paused = await launcher.launch(request)
        assert paused.status is BatchStatus.AWAITING_VALIDATION
        failed = await operator.submit_validation_decision(
            paused.open_validation_id, ValidationDecision.approve(Actor.human("lead"))
        )
        assert failed.status is BatchStatus.FAILED
        assert failed.failures[-1].message == "payment API unavailable"

        Charge.fail = False
        restarted = await operator.restart(failed.instance_id, actor=Actor.human("lead"))
        assert restarted.status is BatchStatus.COMPLETED
        assert (await launcher.launch(request)).id == restarted.id  # duplicate delivery

        # The second tenant's store saw none of it.
        assert await stores[1].list_runs() == []
        assert len(await store.list_runs()) == 2
    finally:
        for store, name in zip(stores, names):
            client = store._client  # noqa: SLF001 - delete only this unique probe DB
            if client is not None:
                await client.drop_database(name)
            await store.close()


async def test_real_mongo_transaction_rollback_and_lease_fencing():
    pytest.importorskip("beanie")
    from etf.stores.beanie_store import BeanieStateStore, MongoLockProvider

    db_name = f"etf_integration_{uuid.uuid4().hex}"
    store = BeanieStateStore(_mongo_uri(), db_name=db_name)
    initialized = False
    try:
        await store.initialize()
        initialized = True
        assert store.supports_transactions, (
            "ETF_REAL_MONGO_URI resolved to a standalone MongoDB; "
            "a replica set or mongos is required"
        )

        instance = JobInstance(
            job_name="transaction_probe",
            identity_hash=uuid.uuid4().hex,
            status=BatchStatus.PENDING,
        )
        with pytest.raises(RuntimeError, match="rollback probe"):
            async with store.transaction():
                await store.create_job_instance(instance)
                raise RuntimeError("rollback probe")
        assert (
            await store.find_job_instance(instance.job_name, instance.identity_hash)
            is None
        )

        assert store._db is not None  # noqa: SLF001 - integration wiring probe
        locks = MongoLockProvider(store._db)  # noqa: SLF001
        await locks.initialize()
        key = f"lease-probe:{uuid.uuid4().hex}"
        first = await locks.acquire(key, "owner-a", timedelta(milliseconds=200))
        assert first is not None
        assert await locks.acquire(key, "owner-b", timedelta(seconds=1)) is None

        await asyncio.sleep(0.3)
        second = await locks.acquire(key, "owner-b", timedelta(seconds=1))
        assert second is not None
        assert first.fencing_token is not None
        assert second.fencing_token is not None
        assert second.fencing_token > first.fencing_token
        assert await first.refresh(timedelta(seconds=1)) is False
        await second.release()
    finally:
        client = store._client  # noqa: SLF001 - delete only this unique probe DB
        if initialized and client is not None:
            await client.drop_database(db_name)
        await store.close()
