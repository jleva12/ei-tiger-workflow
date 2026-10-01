"""Instances carry the labels of the launch that created them, and a host lists them
by label, job name and status, newest first, with the total that match — on every
store (in memory, Mongo through mongomock, and the paging default any store gets)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from etf import InstanceQuery, JobInstance, JobParameters, LaunchRequest, StateStore
from etf.status import BatchStatus
from etf.stores.memory import InMemoryStateStore
from helpers import RecordingStep, make_harness


async def test_a_launch_labels_the_instance_it_creates():
    launcher, _, _, store = make_harness(RecordingStep("work"))
    run = await launcher.launch(
        LaunchRequest(
            job_name="job",
            parameters=JobParameters(identifying={"id": "1"}),
            labels={"tenant": "org-1", "task_type": "workflows"},
        )
    )
    instance = await store.get_job_instance(run.instance_id)
    assert instance.labels == {"tenant": "org-1", "task_type": "workflows"}

    page = await store.query_instances(InstanceQuery(labels={"tenant": "org-1"}))
    assert [i.id for i in page.items] == [instance.id] and page.total == 1
    assert (await store.query_instances(InstanceQuery(labels={"tenant": "org-2"}))).total == 0


async def test_labels_must_be_plain_strings():
    launcher, _, _, _ = make_harness(RecordingStep("work"))
    with pytest.raises(ValueError, match="labels"):
        await launcher.launch(LaunchRequest(job_name="job", labels={"a=b": "c"}))


def _instances() -> list[JobInstance]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def instance(n: int, job: str, status: BatchStatus, tenant: str) -> JobInstance:
        return JobInstance(
            id=f"i{n}",
            job_name=job,
            identity_hash=f"h{n}",
            labels={"tenant": tenant, "task_type": job.split(".")[0]},
            status=status,
            created_at=start + timedelta(minutes=n),
        )

    return [
        instance(1, "workflows.run", BatchStatus.COMPLETED, "t1"),
        instance(2, "workflows.sweep", BatchStatus.FAILED, "t1"),
        instance(3, "invoices.record", BatchStatus.FAILED, "t1"),
        instance(4, "invoices.record", BatchStatus.COMPLETED, "t2"),
        instance(5, "invoicesx.other", BatchStatus.RUNNING, "t1"),
    ]


class PagingOnly(InMemoryStateStore):
    """A store without its own query: it gets the paging default."""

    query_instances = StateStore.query_instances


async def _mongo_store():
    pytest.importorskip("beanie")
    mongomock_motor = pytest.importorskip("mongomock_motor")
    from etf.stores.beanie_store import BeanieStateStore

    store = BeanieStateStore(
        "mongodb://mock", "etf_labels", client=mongomock_motor.AsyncMongoMockClient(), use_transactions=False
    )
    await store.initialize()
    return store


STORES = {
    "memory": InMemoryStateStore,
    "paging-default": PagingOnly,
    "mongo": _mongo_store,
}


@pytest.fixture(params=list(STORES))
async def store(request):
    factory = STORES[request.param]
    made = factory()
    store = await made if hasattr(made, "__await__") else made
    for instance in _instances():
        await store.create_job_instance(instance)
    return store


async def _ids(store, **query) -> tuple[list[str], int]:
    page = await store.query_instances(InstanceQuery(**query))
    return [i.id for i in page.items], page.total


async def test_query_filters_combine_and_come_newest_first(store):
    assert await _ids(store, labels={"tenant": "t1"}) == (["i5", "i3", "i2", "i1"], 4)
    assert await _ids(store, labels={"tenant": "t1", "task_type": "invoices"}) == (["i3"], 1)
    # A prefix is a prefix: "invoices." does not match "invoicesx.".
    assert await _ids(store, job_name_prefixes=["invoices."]) == (["i4", "i3"], 2)
    assert await _ids(store, job_name_prefixes=["workflows.", "invoices."], labels={"tenant": "t1"}) == (
        ["i3", "i2", "i1"],
        3,
    )
    assert await _ids(store, statuses=[BatchStatus.FAILED], labels={"tenant": "t1"}) == (["i3", "i2"], 2)
    assert await _ids(store, job_names=["workflows.run", "invoices.record"]) == (["i4", "i3", "i1"], 3)
    assert await _ids(store, job_name_prefixes=[]) == ([], 0)
    # Leaving a family out: again a prefix, so "invoicesx." stays.
    assert await _ids(store, exclude_job_name_prefixes=["invoices."], labels={"tenant": "t1"}) == (
        ["i5", "i2", "i1"],
        3,
    )
    assert await _ids(
        store, job_name_prefixes=["workflows.", "invoices."], exclude_job_name_prefixes=["workflows."]
    ) == (["i4", "i3"], 2)
    assert await _ids(store, exclude_job_name_prefixes=[], labels={"tenant": "t2"}) == (["i4"], 1)


async def test_query_pages_with_the_total(store):
    assert await _ids(store, labels={"tenant": "t1"}, limit=2) == (["i5", "i3"], 4)
    assert await _ids(store, labels={"tenant": "t1"}, limit=2, offset=2) == (["i2", "i1"], 4)
    assert await _ids(store, labels={"tenant": "t1"}, limit=2, offset=4) == ([], 4)
