"""The workflow runs chat agents' intakes start, each exactly once: the
start's outbox (``workflow_start_intents``), and its upkeep.

An intake completing (its record, kept in the conversation's state) or a
request being submitted (``open`` → ``submitting``, kept in
``intake_requests``) starts the workflows on the intake's ``on_complete``
way. The runs are written down here first, as **intended**, under the ID of
the run each makes (made from the record or request and the workflow, never
a model's call ID); the change that completes the record or submits the
request is kept after; only then does the turn start them
(``OrganizationWorkflows.dispatch``), and each is **dispatched**. Starting
one again finds its run.

A process that stops between keeping the change and starting the runs
leaves them intended. The upkeep (:class:`StartUpkeep`, every process
of the admin API) starts those whose change was kept, once they're older
than a grace period, and drops those whose change never was (its turn
ended without keeping it, so its runs must never start): a request that's
still ``open``, a record the conversation's state doesn't have with its run.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from forge_agent_runtime.intake import STARTING
from forge_agent_runtime.leases import TurnKey
from forge_agent_runtime.services import RequestConflict
from google.adk.sessions import BaseSessionService
from google.adk.sessions.base_session_service import GetSessionConfig
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.db.audit import utc_now
from forge_admin.models import ChatTurnLease, IntakeRequest, WorkflowStartIntent

if TYPE_CHECKING:
    from forge_admin.chat_agents.source import OrganizationWorkflows

logger = logging.getLogger(__name__)

INTENDED, DISPATCHED, DROPPED = "intended", "dispatched", "dropped"

#: Seconds an intended start is left to the turn that wrote it before the
#: upkeep looks at it: that turn keeps its change and starts it in far less.
GRACE_SECONDS = 120.0
#: Seconds a process starting an intent has it before another may.
CLAIM_SECONDS = 60.0
#: Seconds between the upkeep's looks.
UPKEEP_SECONDS = 30.0
#: The most intents one look takes.
UPKEEP_BATCH = 50


def intent_of(row: WorkflowStartIntent) -> dict[str, Any]:
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "agent_id": row.agent_id,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "call_id": row.call_id,
        "workflow": row.workflow,
        "workflow_name": row.workflow_name,
        "input": row.input,
        "origin": row.origin,
        "source": row.source,
        "source_id": row.source_id,
        "state_key": row.state_key,
        "status": row.status,
        "attempts": row.attempts,
        "created_at": row.created_at,
    }


class StartIntents:
    """The workflow starts written down and not made yet, in the admin database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sessions = sessions
        self.clock = clock

    async def write(self, intents: Iterable[dict[str, Any]]) -> None:
        """
        Writes starts down, as intended. One written already is left as it
        is once dispatched; otherwise it's written again as now asked (the
        change that wrote it first was never kept, and it's asked again).
        """
        for intent in intents:
            async with self.sessions() as session:
                row = await session.get(WorkflowStartIntent, intent["id"])
                if row is not None and row.status == DISPATCHED:
                    continue
                if row is None:
                    session.add(WorkflowStartIntent(**intent, status=INTENDED))
                else:
                    for key, value in intent.items():
                        setattr(row, key, value)
                    row.status = INTENDED
                    row.claimed_until = None
                    row.last_error = None
                try:
                    await session.commit()
                except IntegrityError:
                    # Another turn wrote it at the same moment: it's there.
                    await session.rollback()

    async def get(self, run_id: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(WorkflowStartIntent, run_id)
            return intent_of(row) if row is not None else None

    async def dispatched(self, run_id: str, *, error: str | None = None) -> None:
        """Marks a start made (or refused for good, ``error`` saying why)."""
        async with self.sessions() as session:
            await session.execute(
                update(WorkflowStartIntent)
                .where(WorkflowStartIntent.id == run_id)
                .values(
                    status=DISPATCHED,
                    dispatched_at=self.clock(),
                    claimed_until=None,
                    last_error=error,
                )
            )
            await session.commit()

    async def failed(self, run_id: str, error: str) -> None:
        """Notes why a start couldn't be made for now: it's tried again."""
        async with self.sessions() as session:
            await session.execute(
                update(WorkflowStartIntent)
                .where(WorkflowStartIntent.id == run_id)
                .values(last_error=error[:2000])
            )
            await session.commit()

    async def dropped(self, run_id: str) -> None:
        """Marks a start never to be made: the change it waited for wasn't kept."""
        async with self.sessions() as session:
            await session.execute(
                update(WorkflowStartIntent)
                .where(
                    WorkflowStartIntent.id == run_id,
                    WorkflowStartIntent.status == INTENDED,
                )
                .values(status=DROPPED, claimed_until=None)
            )
            await session.commit()

    async def due(
        self,
        *,
        grace: float = GRACE_SECONDS,
        claim: float = CLAIM_SECONDS,
        limit: int = UPKEEP_BATCH,
    ) -> list[dict[str, Any]]:
        """
        :return: Intended starts older than ``grace``, each now claimed by
            the caller for ``claim`` seconds (no other process takes it meanwhile).
        """
        now = self.clock()
        async with self.sessions() as session:
            found = list(
                await session.scalars(
                    select(WorkflowStartIntent)
                    .where(
                        WorkflowStartIntent.status == INTENDED,
                        WorkflowStartIntent.created_at < now - timedelta(seconds=grace),
                        or_(
                            WorkflowStartIntent.claimed_until.is_(None),
                            WorkflowStartIntent.claimed_until < now,
                        ),
                    )
                    .order_by(WorkflowStartIntent.created_at)
                    .limit(limit)
                )
            )
        claimed = []
        for row in found:
            async with self.sessions() as session:
                taken = await session.execute(
                    update(WorkflowStartIntent)
                    .where(
                        WorkflowStartIntent.id == row.id,
                        WorkflowStartIntent.status == INTENDED,
                        or_(
                            WorkflowStartIntent.claimed_until.is_(None),
                            WorkflowStartIntent.claimed_until < now,
                        ),
                    )
                    .values(
                        claimed_until=now + timedelta(seconds=claim),
                        attempts=WorkflowStartIntent.attempts + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            if taken.rowcount == 1:  # type: ignore[attr-defined]
                claimed.append(intent_of(row))
        return claimed


class StartUpkeep:
    """
    Starts the intended workflow runs whose change was kept and whose turn
    didn't start them, and drops those whose change never was.

    :param workflows: Starts them (``OrganizationWorkflows.dispatch``).
    :param chat_sessions: The chat agents' conversations: a record's change
        is kept when its conversation's state has the record with its run.
    """

    def __init__(
        self,
        workflows: "OrganizationWorkflows",
        intents: StartIntents,
        sessions: async_sessionmaker[AsyncSession],
        chat_sessions: BaseSessionService,
        *,
        grace: float = GRACE_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.workflows = workflows
        self.intents = intents
        self.sessions = sessions
        self.chat_sessions = chat_sessions
        self.grace = grace
        self.clock = clock

    async def run_forever(self, every: float = UPKEEP_SECONDS) -> None:
        """Looks every ``every`` seconds (the first time after that long) until cancelled."""
        while True:
            await asyncio.sleep(every)
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Starting the intakes' workflow runs left over failed")

    async def once(self) -> int:
        """:return: How many starts it made or dropped."""
        done = 0
        for intent in await self.intents.due(grace=self.grace):
            kept = await self._kept(intent)
            if kept is None:
                # Its turn still runs: it may keep the change yet.
                continue
            if not kept:
                logger.info(
                    "Dropped the start of run %s: %s %s was never kept",
                    intent["id"],
                    intent["source"],
                    intent["source_id"],
                )
                await self.intents.dropped(intent["id"])
                done += 1
                continue
            try:
                answer = await self.workflows.dispatch(
                    intent["id"], organization_id=intent["organization_id"], wait=0
                )
            except Exception as error:  # noqa: BLE001 - it's tried again at the next look
                logger.warning("Couldn't start run %s yet: %s", intent["id"], error)
                await self.intents.failed(
                    intent["id"], str(error) or type(error).__name__
                )
                continue
            logger.info("Started run %s, left over by its turn", intent["id"])
            if intent["source"] == "request":
                await self._submitted(intent, answer)
            done += 1
        return done

    async def _kept(self, intent: dict[str, Any]) -> bool | None:
        """
        :return: Whether the change the start waited for was kept; None
            while the conversation's turn still runs (it may keep it yet).
        """
        if intent["source"] == "request":
            async with self.sessions() as session:
                row = await session.get(IntakeRequest, intent["source_id"])
            if row is not None and _has_run(row.record, intent["id"]):
                return row.status in ("submitting", "submitted")
        else:
            try:
                session = await self.chat_sessions.get_session(
                    app_name=intent["agent_id"],
                    user_id=intent["user_id"],
                    session_id=intent["session_id"],
                    config=GetSessionConfig(num_recent_events=0),
                )
            except (SQLAlchemyError, ValueError):
                return None
            record = (
                session.state.get(intent["state_key"] or "")
                if session is not None
                else None
            )
            if (
                isinstance(record, dict)
                and record.get("id") == intent["source_id"]
                and _has_run(record, intent["id"])
            ):
                return True
        return None if await self._turn_running(intent) else False

    async def _turn_running(self, intent: dict[str, Any]) -> bool:
        """:return: Whether a turn holds the conversation the start was written in."""
        from forge_admin.chat_agents.turn_leases import lease_id

        key = TurnKey(intent["agent_id"], intent["user_id"], intent["session_id"])
        async with self.sessions() as session:
            lease = await session.get(ChatTurnLease, lease_id(key))
        return lease is not None and lease.expires_at > self.clock()

    async def _submitted(self, intent: dict[str, Any], answer: dict[str, Any]) -> None:
        """A request whose runs the upkeep started: each run as it got to,
        submitted once none waits."""
        for _ in range(3):
            async with self.sessions() as session:
                row = await session.get(IntakeRequest, intent["source_id"])
                if row is None or row.status != "submitting":
                    return
                record = dict(row.record)
                workflows = [
                    {**entry, **answer}
                    if entry.get("run_id") == intent["id"]
                    else entry
                    for entry in record.get("workflows") or []
                ]
                record["workflows"] = workflows
                status = (
                    "submitting"
                    if any(entry.get("status") == STARTING for entry in workflows)
                    else "submitted"
                )
                values: dict[str, Any] = {
                    "record": record,
                    "status": status,
                    "version": row.version + 1,
                }
                if status == "submitted":
                    values["submitted_at"] = utc_now()
                changed = await session.execute(
                    update(IntakeRequest)
                    .where(
                        IntakeRequest.id == row.id,
                        IntakeRequest.version == row.version,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            if changed.rowcount == 1:  # type: ignore[attr-defined]
                return
        raise RequestConflict(f"Request {intent['source_id']} kept changing")


def _has_run(record: Any, run_id: str) -> bool:
    """:return: Whether a record (an intake's, a request's) says it starts the run."""
    workflows = record.get("workflows") if isinstance(record, dict) else None
    return any(
        isinstance(entry, dict) and entry.get("run_id") == run_id
        for entry in workflows or []
    )
