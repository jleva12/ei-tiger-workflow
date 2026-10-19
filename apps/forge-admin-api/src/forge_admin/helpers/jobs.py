"""Background helpers' jobs (``forge_admin.models.HelperJob``).

A helper with ``run: background`` doesn't keep its request waiting: the
request makes its job and starts it as a task in this process
(:func:`start`), which runs the helper (:func:`.executor.perform`) and puts
what it made, or why it couldn't, on the job. A job that should have
finished long ago stopped with the process it ran in: :func:`listed` says
it failed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from google.adk.models import BaseLlm
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.db.audit import utc_now
from forge_admin.helpers.executor import Helper, perform
from forge_admin.helpers.kit import HelperError, HelperRun
from forge_admin.models import HelperJob

logger = logging.getLogger(__name__)

#: How long after it should have finished a running job is taken to have stopped.
GRACE = timedelta(seconds=120)
#: How long jobs stay listed.
KEPT = timedelta(days=7)
#: The most jobs listed.
MOST = 50
STOPPED = "It stopped: Forge restarted while it was working. Try again."


def _tasks(state: Any) -> set[asyncio.Task[None]]:
    """The jobs running in this process, kept so they aren't collected."""
    tasks: set[asyncio.Task[None]] | None = getattr(state, "helper_tasks", None)
    if tasks is None:
        tasks = set()
        state.helper_tasks = tasks
    return tasks


async def start(
    session: AsyncSession,
    helper: Helper,
    run: HelperRun,
    given: BaseModel,
    *,
    message: str,
    model: BaseLlm,
    **usage: Any,
) -> HelperJob:
    """
    Makes the job and starts it beside the request.

    :param session: The request's: the job is committed before it starts.
    :param usage: :func:`.executor.perform`'s ``attribution``, ``usage`` and ``price``.
    :return: The job, running.
    """
    job = HelperJob(
        organization_id=run.organization_id,
        helper=helper.name,
        input=given.model_dump(mode="json"),
        status="running",
        notes=[],
    )
    session.add(job)
    await session.commit()
    run.job_id = job.id
    task = asyncio.create_task(
        _run(helper, run, given, message=message, model=model, **usage),
        name=f"helper:{helper.name}:{job.id}",
    )
    tasks = _tasks(run.state)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return job


async def _run(
    helper: Helper,
    run: HelperRun,
    given: BaseModel,
    *,
    message: str,
    model: BaseLlm,
    **usage: Any,
) -> None:
    """A job, start to end: what it made on the job, or why not."""
    try:
        finished = await perform(
            helper, run, given, message=message, model=model, **usage
        )
        changes: dict[str, Any] = {
            "status": "done",
            "result": finished.result,
            "notes": list(finished.notes),
            "model": finished.model[:200] or None,
        }
        logger.info(
            "The %s helper finished a job for %s in organization %s",
            helper.name,
            run.user_id,
            run.organization_id,
        )
    except HelperError as error:
        changes = {"status": "failed", "error": str(error)}
    try:
        async with run.sessions() as session:
            job = await session.get(HelperJob, run.job_id)
            if job is not None:
                for name, value in changes.items():
                    setattr(job, name, value)
                await session.commit()
    except Exception:
        logger.exception("Couldn't record how the %s helper's job went", helper.name)


async def listed(
    session: AsyncSession, organization_id: str, helper: Helper
) -> list[HelperJob]:
    """
    :return: The helper's jobs in the organization from the last week, newest
        first; one that should have finished long ago marked failed.
    """
    now = utc_now()
    rows = list(
        await session.scalars(
            select(HelperJob)
            .where(
                HelperJob.organization_id == organization_id,
                HelperJob.helper == helper.name,
                HelperJob.created_at >= now - KEPT,
            )
            .order_by(HelperJob.created_at.desc())
            .limit(MOST)
        )
    )
    late = now - timedelta(seconds=helper.spec.seconds) - GRACE
    stale = [row for row in rows if row.status == "running" and row.created_at < late]
    for row in stale:
        row.status = "failed"
        row.error = STOPPED
    if stale:
        await session.commit()
    return rows


async def dismiss(
    session: AsyncSession, organization_id: str, helper: Helper, job_id: str
) -> bool:
    """
    Takes a job off the list; one still running goes on.

    :return: Whether the organization had it.
    """
    found = await session.execute(
        delete(HelperJob).where(
            HelperJob.id == job_id,
            HelperJob.organization_id == organization_id,
            HelperJob.helper == helper.name,
        )
    )
    await session.commit()
    return bool(getattr(found, "rowcount", 0))
