"""``BeanieStateStore`` — the default MongoDB-backed :class:`~etf.store.StateStore`.

Requires the optional ``mongo`` extra (``pip install 'enhanced-task-framework[mongo]'``,
which pulls in ``beanie`` + ``motor``). The framework core does not import this module, so
ETF runs fine without MongoDB installed; importing ``BeanieStateStore`` without the extra
raises a clear ``ImportError``.

Design notes
------------
* Each domain entity maps to one Beanie ``Document``. The frequently-queried fields are
  promoted to real indexed fields; richer nested structures (parameters, execution
  context, failures, exit status) are stored as embedded dicts for schema flexibility.
* **Optimistic concurrency** is implemented with an atomic ``find_one_and_update`` gated
  on ``version`` — a compare-and-set that raises :class:`~etf.exceptions.OptimisticLockError`
  on mismatch.
* **Instance identity** and **idempotency keys** rely on unique indexes; duplicate-key
  errors are translated into the framework's typed exceptions.
* A TTL index on the idempotency collection lets keys expire automatically.

Companion :class:`MongoLockProvider` (also in this module) implements the
:class:`~etf.locking.RunLockProvider` using a TTL-leased lock collection with monotonic
fencing tokens.
"""

# pyright: reportPossiblyUnboundVariable=false, reportArgumentType=false

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from ..audit import AuditEvent, AuditQuery
from ..exceptions import JobInstanceAlreadyExistsError, OptimisticLockError
from ..model import (
    Actor,
    FailureRecord,
    JobInstance,
    JobParameters,
    JobRun,
    StepRun,
    ValidationRequest,
)
from ..status import (
    ActorKind,
    AuditEventType,
    BatchStatus,
    ExitStatus,
    ValidationDecisionType,
    ValidationStatus,
)
from ..store import InstancePage, InstanceQuery, StateStore

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Mongo may return naive datetimes; normalize reads back to tz-aware UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Pure mapping helpers (no beanie dependency — importable & unit-testable anywhere)
# --------------------------------------------------------------------------- #
def actor_to_dict(actor: Actor | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {
        "kind": actor.kind.value,
        "id": actor.id,
        "display_name": actor.display_name,
        "roles": sorted(actor.roles),
    }


def actor_from_dict(d: dict[str, Any] | None) -> Actor | None:
    if not d:
        return None
    return Actor(
        ActorKind(d["kind"]), d["id"], d.get("display_name", ""), frozenset(d.get("roles", ()))
    )


def exit_to_dict(es: ExitStatus | None) -> dict[str, Any] | None:
    if es is None:
        return None
    return {"code": es.code, "description": es.description, "exit_attributes": dict(es.exit_attributes)}


def exit_from_dict(d: dict[str, Any] | None) -> ExitStatus:
    if not d:
        return ExitStatus("UNKNOWN")
    return ExitStatus(d["code"], d.get("description", ""), dict(d.get("exit_attributes", {})))


def failure_to_dict(f: FailureRecord) -> dict[str, Any]:
    return {
        "exception_type": f.exception_type,
        "message": f.message,
        "stack_trace": f.stack_trace,
        "cause_chain": list(f.cause_chain),
        "step_name": f.step_name,
        "attempt": f.attempt,
        "retryable": f.retryable,
        "fatal": f.fatal,
        "occurred_at": f.occurred_at,
        "attributes": dict(f.attributes),
    }


def failure_from_dict(d: dict[str, Any]) -> FailureRecord:
    return FailureRecord(
        exception_type=d["exception_type"],
        message=d["message"],
        stack_trace=d.get("stack_trace", ""),
        cause_chain=list(d.get("cause_chain", [])),
        step_name=d.get("step_name"),
        attempt=d.get("attempt", 0),
        retryable=d.get("retryable", False),
        fatal=d.get("fatal", False),
        occurred_at=_aware(d.get("occurred_at")) or _utcnow(),
        attributes=dict(d.get("attributes", {})),
    )


def params_to_dict(p: JobParameters) -> dict[str, Any]:
    return {"identifying": dict(p.identifying), "non_identifying": dict(p.non_identifying)}


def params_from_dict(d: dict[str, Any] | None) -> JobParameters:
    d = d or {}
    return JobParameters(
        identifying=dict(d.get("identifying", {})),
        non_identifying=dict(d.get("non_identifying", {})),
    )


try:
    import pymongo
    from beanie import Document, init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
    from pymongo.errors import DuplicateKeyError, PyMongoError

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc


if _IMPORT_ERROR is None:
    # ------------------------------------------------------------------ #
    # Beanie documents
    # ------------------------------------------------------------------ #
    class _Documents:
        """One store's own Beanie Document classes.

        Beanie binds a Document class to a single database — and initializing a
        subclass initializes its parents too — so stores sharing classes would all read
        and write whichever database was bound last. Each store defines its own classes
        (same collections and indexes) and binds them to its own database, so several
        stores — one per tenant database, say — can share a process.
        """

        def __init__(self) -> None:
            class JobInstanceDoc(Document):
                etf_id: str
                job_name: str
                identity_hash: str
                definition_version: int = 1
                identifying_parameters: dict[str, Any] = {}
                labels: dict[str, str] = {}
                # The labels as "key=value" strings: one multikey index serves any label filter.
                label_pairs: list[str] = []
                status: str = BatchStatus.PENDING.value
                created_at: datetime
                updated_at: datetime
                version: int = 0

                class Settings:
                    name = "etf_job_instances"
                    indexes = [
                        pymongo.IndexModel(
                            [("job_name", 1), ("identity_hash", 1)], unique=True, name="uniq_identity"
                        ),
                        pymongo.IndexModel([("etf_id", 1)], unique=True, name="uniq_etf_id"),
                        pymongo.IndexModel([("status", 1)]),
                        pymongo.IndexModel([("label_pairs", 1), ("created_at", -1)], name="labels_newest"),
                        pymongo.IndexModel([("created_at", -1)], name="newest"),
                    ]

            class JobRunDoc(Document):
                etf_id: str
                instance_id: str
                job_name: str
                definition_version: int = 1
                definition_fingerprint: str | None = None
                status: str = BatchStatus.PENDING.value
                exit_status: dict[str, Any] | None = None
                attempt: int = 1
                restart_of: str | None = None
                current_step: str | None = None
                parameters: dict[str, Any] = {}
                execution_context: Any = {}
                failures: list[dict[str, Any]] = []
                open_validation_id: str | None = None
                requested_by: dict[str, Any] | None = None
                correlation_id: str | None = None
                trace_id: str | None = None
                idempotency_key: str | None = None
                create_time: datetime
                start_time: datetime | None = None
                end_time: datetime | None = None
                last_updated: datetime
                version: int = 0

                class Settings:
                    name = "etf_job_runs"
                    indexes = [
                        pymongo.IndexModel([("etf_id", 1)], unique=True, name="uniq_etf_id"),
                        pymongo.IndexModel(
                            [("instance_id", 1), ("attempt", -1)], unique=True, name="uniq_instance_attempt"
                        ),
                        pymongo.IndexModel([("job_name", 1), ("status", 1)]),
                        # Serves the crash-recovery sweep (recover_stale_runs): stale runs by
                        # status ordered by staleness.
                        pymongo.IndexModel([("status", 1), ("last_updated", 1)], name="status_staleness"),
                    ]

            class StepRunDoc(Document):
                etf_id: str
                run_id: str
                instance_id: str
                step_name: str
                status: str = BatchStatus.PENDING.value
                exit_status: dict[str, Any] | None = None
                attempt: int = 1
                execution_context: Any = {}
                read_count: int = 0
                write_count: int = 0
                skip_count: int = 0
                commit_count: int = 0
                attributes: dict[str, Any] = {}
                failures: list[dict[str, Any]] = []
                start_time: datetime | None = None
                end_time: datetime | None = None
                updated_at: datetime
                version: int = 0

                class Settings:
                    name = "etf_step_runs"
                    indexes = [
                        pymongo.IndexModel([("etf_id", 1)], unique=True, name="uniq_etf_id"),
                        pymongo.IndexModel([("run_id", 1), ("step_name", 1)], unique=True, name="uniq_run_step"),
                        pymongo.IndexModel([("instance_id", 1), ("step_name", 1), ("updated_at", -1)]),
                    ]

            class ValidationRequestDoc(Document):
                etf_id: str
                run_id: str
                instance_id: str
                step_name: str
                reason: str
                payload: dict[str, Any] = {}
                decision_schema: dict[str, Any] = {}
                required_role: str | None = None
                status: str = ValidationStatus.PENDING.value
                created_at: datetime
                sla_deadline: datetime | None = None
                decided_at: datetime | None = None
                decision_type: str | None = None
                decision_actor_id: str | None = None
                decision_comment: str = ""
                decision_overrides: dict[str, Any] = {}
                version: int = 0

                class Settings:
                    name = "etf_validation_requests"
                    indexes = [
                        pymongo.IndexModel([("etf_id", 1)], unique=True, name="uniq_etf_id"),
                        pymongo.IndexModel([("run_id", 1), ("status", 1)]),
                        pymongo.IndexModel([("status", 1), ("required_role", 1)]),
                        pymongo.IndexModel([("status", 1), ("sla_deadline", 1)], name="status_sla_deadline"),
                    ]

            class AuditEventDoc(Document):
                etf_id: str
                event_type: str
                run_id: str
                instance_id: str
                sequence: int
                at: datetime
                actor: dict[str, Any] | None = None
                step_name: str | None = None
                from_status: str | None = None
                to_status: str | None = None
                exit_status: dict[str, Any] | None = None
                failure: dict[str, Any] | None = None
                message: str = ""
                attributes: dict[str, Any] = {}
                correlation_id: str | None = None
                trace_id: str | None = None

                class Settings:
                    name = "etf_audit_events"
                    indexes = [
                        pymongo.IndexModel([("etf_id", 1)], unique=True, name="uniq_etf_id"),
                        pymongo.IndexModel([("run_id", 1), ("sequence", 1)], unique=True, name="uniq_run_seq"),
                        pymongo.IndexModel([("instance_id", 1)]),
                        pymongo.IndexModel([("event_type", 1)]),
                        pymongo.IndexModel([("at", 1)]),
                    ]

            class IdempotencyKeyDoc(Document):
                key: str
                run_id: str
                created_at: datetime
                expires_at: datetime

                class Settings:
                    name = "etf_idempotency_keys"
                    indexes = [
                        pymongo.IndexModel([("key", 1)], unique=True, name="uniq_key"),
                        pymongo.IndexModel([("expires_at", 1)], expireAfterSeconds=0, name="ttl_expires"),
                    ]

            self.JobInstanceDoc = JobInstanceDoc
            self.JobRunDoc = JobRunDoc
            self.StepRunDoc = StepRunDoc
            self.ValidationRequestDoc = ValidationRequestDoc
            self.AuditEventDoc = AuditEventDoc
            self.IdempotencyKeyDoc = IdempotencyKeyDoc

        def all(self) -> list[type[Document]]:
            return [
                self.JobInstanceDoc, self.JobRunDoc, self.StepRunDoc,
                self.ValidationRequestDoc, self.AuditEventDoc, self.IdempotencyKeyDoc,
            ]

    # ------------------------------------------------------------------ #
    # The store
    # ------------------------------------------------------------------ #
    class BeanieStateStore(StateStore):
        """MongoDB-backed state store. Construct with a Mongo URI + database name."""

        def __init__(
            self,
            uri: str,
            db_name: str = "etf",
            *,
            client: Any = None,
            use_transactions: bool = True,
            commit_retry_attempts: int = 5,
            commit_retry_base_delay: float = 0.1,
        ) -> None:
            self._uri = uri
            self._db_name = db_name
            self._client = client
            self._db: AsyncIOMotorDatabase | None = None
            self._use_transactions = use_transactions
            self._commit_retry_attempts = commit_retry_attempts
            self._commit_retry_base_delay = commit_retry_base_delay
            self._docs = _Documents()
            self._loop: asyncio.AbstractEventLoop | None = None
            self._session: ContextVar[Any | None] = ContextVar(
                f"etf_mongo_session_{id(self)}", default=None
            )
            self._after_commit: ContextVar[list[Any] | None] = ContextVar(
                f"etf_mongo_after_commit_{id(self)}", default=None
            )

        # -- lifecycle --
        async def initialize(self) -> None:
            running_loop = asyncio.get_running_loop()
            if self._loop is not None and self._loop is not running_loop:
                raise RuntimeError(
                    "BeanieStateStore was already initialized on a different event loop"
                )
            self._client = self._client or AsyncIOMotorClient(self._uri, tz_aware=True)
            self._db = self._client[self._db_name]
            # Motor clients are bound to the loop they are created/used on; remember it
            # so cross-loop use fails with a clear message (see _assert_loop).
            self._loop = running_loop
            await init_beanie(database=self._db, document_models=self._docs.all())
            await self._detect_transaction_support()

        async def _detect_transaction_support(self) -> None:
            """Transactions need a replica set or mongos; on a standalone mongod every
            ``transaction()`` would throw, so detect the topology and degrade loudly."""
            if not self._use_transactions:
                return
            assert self._db is not None
            try:
                hello = await self._db.command("hello")
            except Exception:  # server too old for `hello`, or a permissive test double
                return
            if "setName" not in hello and hello.get("msg") != "isdbgrid":
                logger.warning(
                    "MongoDB at %s is a standalone server: multi-document transactions "
                    "are unavailable; disabling use_transactions (state writes will not "
                    "be atomic across documents)", self._uri,
                )
                self._use_transactions = False

        def _assert_loop(self) -> None:
            """Fail fast with a clear message instead of Motor's cryptic
            'attached to a different loop' error (see AsyncBridge docs)."""
            if self._loop is None:
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover - store calls are always async
                return
            if running is not self._loop:
                raise RuntimeError(
                    "BeanieStateStore was initialized on a different event loop. The "
                    "Motor client is loop-bound: run store.initialize() and every ETF "
                    "call through the same loop (from synchronous code, the same AsyncBridge)."
                )

        async def close(self) -> None:
            self._assert_loop()
            if self._client is not None:
                self._client.close()

        async def healthcheck(self) -> bool:
            assert self._db is not None
            self._assert_loop()
            await self._db.command("ping")
            return True

        @property
        def supports_transactions(self) -> bool:
            return self._use_transactions

        @asynccontextmanager
        async def transaction(self):
            """Multi-document transaction scope (nested scopes join the outer one).

            The commit is retried with backoff on ``UnknownTransactionCommitResult``.
            A ``TransientTransactionError`` raised mid-body cannot be replayed by a
            context manager (the body already ran) and propagates — callers that want
            the full standard retry pattern should use :meth:`run_in_transaction`.
            """
            self._assert_loop()
            callbacks = self._after_commit.get()
            if callbacks is not None:
                # Nested scopes join the outer transaction, including non-Mongo scopes.
                yield
                return
            callback_list: list[Any] = []
            callback_token = self._after_commit.set(callback_list)
            if not self._use_transactions:
                try:
                    yield
                finally:
                    self._after_commit.reset(callback_token)
                await self._run_after_commit(callback_list)
                return
            assert self._client is not None
            try:
                async with await self._client.start_session() as session:
                    token = self._session.set(session)
                    try:
                        session.start_transaction()
                        try:
                            yield
                        except BaseException:
                            await session.abort_transaction()
                            raise
                        await self._commit_with_retry(session)
                    finally:
                        self._session.reset(token)
            finally:
                self._after_commit.reset(callback_token)
            await self._run_after_commit(callback_list)

        async def _run_after_commit(self, callbacks: list[Any]) -> None:
            for callback in callbacks:
                try:
                    await callback()
                except Exception:
                    logger.exception("after-commit callback failed")

        def defer_after_commit(self, callback: Any) -> bool:
            callbacks = self._after_commit.get()
            if callbacks is None:
                return False
            callbacks.append(callback)
            return True

        async def _commit_with_retry(self, session: Any) -> None:
            """Standard Mongo commit loop: an UnknownTransactionCommitResult means the
            commit *may or may not* have landed — retrying commit_transaction is safe."""
            for attempt in range(self._commit_retry_attempts):
                try:
                    await session.commit_transaction()
                    return
                except PyMongoError as exc:
                    last = attempt == self._commit_retry_attempts - 1
                    if last or not exc.has_error_label("UnknownTransactionCommitResult"):
                        raise
                    await asyncio.sleep(self._commit_retry_base_delay * 2**attempt)

        async def run_in_transaction(
            self, fn: Callable[[], Awaitable[Any]], *, attempts: int = 3
        ) -> Any:
            """Run ``fn`` inside a transaction with the full standard retry pattern:
            the whole callback is replayed on ``TransientTransactionError`` (commit
            retries are handled inside :meth:`transaction`). ``fn`` must be safe to
            re-run — everything it did in the aborted attempt was rolled back."""
            if self._after_commit.get() is not None:
                # A nested callback belongs to the outer transaction. Let errors
                # escape so the outermost callback—not a poisoned session—is replayed.
                return await fn()
            for attempt in range(attempts):
                try:
                    async with self.transaction():
                        return await fn()
                except PyMongoError as exc:
                    last = attempt == attempts - 1
                    if last or not exc.has_error_label("TransientTransactionError"):
                        raise
                    await asyncio.sleep(self._commit_retry_base_delay * 2**attempt)

        # -- CAS helper --
        async def _cas(self, doc_cls: Any, etf_id: str, expected_version: int, changes: dict[str, Any]) -> dict[str, Any]:
            """Atomic compare-and-set on ``version``; returns the updated raw document."""
            self._assert_loop()
            coll = doc_cls.get_motor_collection()
            updated = await coll.find_one_and_update(
                {"etf_id": etf_id, "version": expected_version},
                {"$set": {**changes, "version": expected_version + 1}},
                return_document=pymongo.ReturnDocument.AFTER,
                session=self._session.get(),
            )
            if updated is None:
                raise OptimisticLockError(f"{doc_cls.__name__} {etf_id} version != {expected_version}")
            return updated

        # -- instances --
        async def create_job_instance(self, instance: JobInstance) -> JobInstance:
            self._assert_loop()
            instance.version = 1
            doc = self._docs.JobInstanceDoc(
                etf_id=instance.id,
                job_name=instance.job_name,
                identity_hash=instance.identity_hash,
                identifying_parameters=dict(instance.identifying_parameters),
                labels=dict(instance.labels),
                label_pairs=[f"{key}={value}" for key, value in instance.labels.items()],
                definition_version=instance.definition_version,
                status=instance.status.value,
                created_at=instance.created_at,
                updated_at=instance.updated_at,
                version=1,
            )
            try:
                await doc.insert(session=self._session.get())
            except DuplicateKeyError as exc:
                raise JobInstanceAlreadyExistsError(
                    f"instance exists for ({instance.job_name}, {instance.identity_hash})"
                ) from exc
            return instance

        async def find_job_instance(self, job_name: str, identity_hash: str) -> JobInstance | None:
            self._assert_loop()
            doc = await self._docs.JobInstanceDoc.find_one(
                self._docs.JobInstanceDoc.job_name == job_name, self._docs.JobInstanceDoc.identity_hash == identity_hash,
                session=self._session.get(),
            )
            return self._to_instance(doc) if doc else None

        async def get_job_instance(self, instance_id: str) -> JobInstance:
            self._assert_loop()
            doc = await self._docs.JobInstanceDoc.find_one(
                self._docs.JobInstanceDoc.etf_id == instance_id, session=self._session.get()
            )
            if doc is None:
                raise KeyError(f"job instance {instance_id} not found")
            return self._to_instance(doc)

        async def update_job_instance(self, instance: JobInstance, expected_version: int) -> JobInstance:
            await self._cas(
                self._docs.JobInstanceDoc, instance.id, expected_version,
                {"status": instance.status.value, "updated_at": instance.updated_at,
                 "identifying_parameters": dict(instance.identifying_parameters),
                 "definition_version": instance.definition_version},
            )
            instance.version = expected_version + 1
            return instance

        async def list_job_instances(self, *, job_name=None, status=None, limit=50, offset=0) -> list[JobInstance]:
            self._assert_loop()
            q = self._docs.JobInstanceDoc.find(session=self._session.get())
            if job_name is not None:
                q = q.find(self._docs.JobInstanceDoc.job_name == job_name)
            if status is not None:
                q = q.find(self._docs.JobInstanceDoc.status == status.value)
            docs = await q.sort("-created_at").skip(offset).limit(limit).to_list()
            return [self._to_instance(d) for d in docs]

        async def query_instances(self, query: InstanceQuery) -> InstancePage:
            self._assert_loop()
            clauses: list[dict[str, Any]] = []
            if query.job_names is not None:
                clauses.append({"job_name": {"$in": list(query.job_names)}})
            if query.job_name_prefixes is not None:
                clauses.append(
                    {"$or": [{"job_name": {"$regex": f"^{re.escape(p)}"}} for p in query.job_name_prefixes]}
                    if query.job_name_prefixes
                    else {"job_name": {"$in": []}}
                )
            if query.exclude_job_name_prefixes:
                clauses.append(
                    {"$nor": [{"job_name": {"$regex": f"^{re.escape(p)}"}} for p in query.exclude_job_name_prefixes]}
                )
            if query.statuses is not None:
                clauses.append({"status": {"$in": [s.value for s in query.statuses]}})
            if query.labels:
                clauses.append({"label_pairs": {"$all": [f"{k}={v}" for k, v in query.labels.items()]}})
            filters: dict[str, Any] = {"$and": clauses} if clauses else {}
            found = self._docs.JobInstanceDoc.find(filters, session=self._session.get())
            total = await found.count()
            docs = await self._docs.JobInstanceDoc.find(filters, session=self._session.get()).sort(
                "-created_at"
            ).skip(query.offset).limit(query.limit).to_list()
            return InstancePage(items=[self._to_instance(d) for d in docs], total=total)

        # -- runs --
        async def create_job_run(self, run: JobRun) -> JobRun:
            self._assert_loop()
            run.version = 1
            await self._docs.JobRunDoc(**self._run_fields(run), version=1).insert(
                session=self._session.get()
            )
            # Pre-create the audit counter here, off the audit hot path: the per-event
            # $inc can then run without upsert, which avoids upsert write conflicts
            # inside transactions.
            assert self._db is not None
            await self._db["etf_counters"].update_one(
                {"_id": run.id},
                {"$setOnInsert": {"seq": 0}},
                upsert=True,
                session=self._session.get(),
            )
            return run

        async def get_job_run(self, run_id: str) -> JobRun:
            self._assert_loop()
            doc = await self._docs.JobRunDoc.find_one(
                self._docs.JobRunDoc.etf_id == run_id, session=self._session.get()
            )
            if doc is None:
                raise KeyError(f"job run {run_id} not found")
            return self._to_run(doc)

        async def update_job_run(self, run: JobRun, expected_version: int) -> JobRun:
            await self._cas(self._docs.JobRunDoc, run.id, expected_version, self._run_fields(run))
            run.version = expected_version + 1
            return run

        async def find_runs_for_instance(self, instance_id: str) -> list[JobRun]:
            self._assert_loop()
            docs = await self._docs.JobRunDoc.find(
                self._docs.JobRunDoc.instance_id == instance_id, session=self._session.get()
            ).sort("attempt").to_list()
            return [self._to_run(d) for d in docs]

        async def find_latest_run(self, instance_id: str) -> JobRun | None:
            self._assert_loop()
            doc = await self._docs.JobRunDoc.find(
                self._docs.JobRunDoc.instance_id == instance_id, session=self._session.get()
            ).sort("-attempt").limit(1).first_or_none()
            return self._to_run(doc) if doc else None

        async def list_runs(self, *, job_name=None, status=None, limit=50, offset=0) -> list[JobRun]:
            self._assert_loop()
            q = self._docs.JobRunDoc.find(session=self._session.get())
            if job_name is not None:
                q = q.find(self._docs.JobRunDoc.job_name == job_name)
            if status is not None:
                q = q.find(self._docs.JobRunDoc.status == status.value)
            docs = await q.sort("-create_time").skip(offset).limit(limit).to_list()
            return [self._to_run(d) for d in docs]

        async def list_stale_runs(
            self,
            *,
            status: BatchStatus,
            updated_before: datetime,
            limit: int = 100,
            offset: int = 0,
        ) -> list[JobRun]:
            self._assert_loop()
            docs = await self._docs.JobRunDoc.find(
                self._docs.JobRunDoc.status == status.value,
                self._docs.JobRunDoc.last_updated <= updated_before,
                session=self._session.get(),
            ).sort("last_updated").skip(offset).limit(limit).to_list()
            return [self._to_run(doc) for doc in docs]

        # -- steps --
        async def create_step_run(self, step_run: StepRun) -> StepRun:
            self._assert_loop()
            step_run.version = 1
            await self._docs.StepRunDoc(**self._step_fields(step_run), version=1).insert(
                session=self._session.get()
            )
            return step_run

        async def get_step_run(self, step_run_id: str) -> StepRun:
            self._assert_loop()
            doc = await self._docs.StepRunDoc.find_one(
                self._docs.StepRunDoc.etf_id == step_run_id, session=self._session.get()
            )
            if doc is None:
                raise KeyError(f"step run {step_run_id} not found")
            return self._to_step(doc)

        async def update_step_run(self, step_run: StepRun, expected_version: int) -> StepRun:
            await self._cas(self._docs.StepRunDoc, step_run.id, expected_version, self._step_fields(step_run))
            step_run.version = expected_version + 1
            return step_run

        async def find_step_runs(self, run_id: str) -> list[StepRun]:
            self._assert_loop()
            docs = await self._docs.StepRunDoc.find(
                self._docs.StepRunDoc.run_id == run_id, session=self._session.get()
            ).sort("updated_at").to_list()
            return [self._to_step(d) for d in docs]

        async def find_step_run(self, run_id: str, step_name: str) -> StepRun | None:
            self._assert_loop()
            doc = await self._docs.StepRunDoc.find_one(
                self._docs.StepRunDoc.run_id == run_id, self._docs.StepRunDoc.step_name == step_name,
                session=self._session.get(),
            )
            return self._to_step(doc) if doc else None

        async def find_last_step_run(self, instance_id, step_name, *, status=None) -> StepRun | None:
            self._assert_loop()
            q = self._docs.StepRunDoc.find(
                self._docs.StepRunDoc.instance_id == instance_id, self._docs.StepRunDoc.step_name == step_name,
                session=self._session.get(),
            )
            if status is not None:
                q = q.find(self._docs.StepRunDoc.status == status.value)
            # _id (monotonic ObjectId) breaks updated_at ties, e.g. under a fixed clock.
            doc = await q.sort(
                [("updated_at", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)]
            ).limit(1).first_or_none()
            return self._to_step(doc) if doc else None

        # -- validation --
        async def create_validation_request(self, request: ValidationRequest) -> ValidationRequest:
            self._assert_loop()
            request.version = 1
            await self._docs.ValidationRequestDoc(**self._vr_fields(request), version=1).insert(
                session=self._session.get()
            )
            return request

        async def get_validation_request(self, request_id: str) -> ValidationRequest:
            self._assert_loop()
            doc = await self._docs.ValidationRequestDoc.find_one(
                self._docs.ValidationRequestDoc.etf_id == request_id, session=self._session.get()
            )
            if doc is None:
                raise KeyError(f"validation request {request_id} not found")
            return self._to_vr(doc)

        async def update_validation_request(
            self, request: ValidationRequest, expected_version: int
        ) -> ValidationRequest:
            await self._cas(
                self._docs.ValidationRequestDoc, request.id, expected_version, self._vr_fields(request)
            )
            request.version = expected_version + 1
            return request

        async def find_open_validation(self, run_id: str) -> ValidationRequest | None:
            self._assert_loop()
            doc = await self._docs.ValidationRequestDoc.find_one(
                self._docs.ValidationRequestDoc.run_id == run_id,
                self._docs.ValidationRequestDoc.status == ValidationStatus.PENDING.value,
                session=self._session.get(),
            )
            return self._to_vr(doc) if doc else None

        async def list_pending_validations(self, *, required_role=None, limit=50, offset=0) -> list[ValidationRequest]:
            self._assert_loop()
            q = self._docs.ValidationRequestDoc.find(
                self._docs.ValidationRequestDoc.status == ValidationStatus.PENDING.value,
                session=self._session.get(),
            )
            if required_role is not None:
                q = q.find(self._docs.ValidationRequestDoc.required_role == required_role)
            docs = await q.sort("created_at").skip(offset).limit(limit).to_list()
            return [self._to_vr(d) for d in docs]

        async def list_overdue_validations(
            self, *, now: datetime, limit: int = 100, offset: int = 0
        ) -> list[ValidationRequest]:
            self._assert_loop()
            if limit <= 0:
                return []
            docs = await (
                self._docs.ValidationRequestDoc.find(
                    {
                        "status": ValidationStatus.PENDING.value,
                        "sla_deadline": {"$ne": None, "$lte": now},
                    },
                    session=self._session.get(),
                )
                .sort("sla_deadline")
                .skip(offset)
                .limit(limit)
                .to_list()
            )
            return [self._to_vr(d) for d in docs]

        # -- idempotency --
        async def register_idempotency_key(self, key, run_id, ttl=None) -> str | None:
            self._assert_loop()
            ttl = ttl or timedelta(days=1)
            now = _utcnow()
            stolen = await self._docs.IdempotencyKeyDoc.get_motor_collection().find_one_and_update(
                {"key": key, "expires_at": {"$lte": now}},
                {
                    "$set": {
                        "run_id": run_id,
                        "created_at": now,
                        "expires_at": now + ttl,
                    }
                },
                session=self._session.get(),
                return_document=pymongo.ReturnDocument.AFTER,
            )
            if stolen is not None:
                return None
            try:
                await self._docs.IdempotencyKeyDoc(
                    key=key, run_id=run_id, created_at=now, expires_at=now + ttl
                ).insert(session=self._session.get())
                return None
            except DuplicateKeyError:
                existing = await self._docs.IdempotencyKeyDoc.find_one(
                    self._docs.IdempotencyKeyDoc.key == key, session=self._session.get()
                )
                return existing.run_id if existing else None

        async def rebind_idempotency_key(
            self, key, expected_run_id, run_id, ttl=None
        ) -> bool:
            self._assert_loop()
            ttl = ttl or timedelta(days=1)
            res = await self._docs.IdempotencyKeyDoc.get_motor_collection().update_one(
                {"key": key, "run_id": expected_run_id},
                {"$set": {"run_id": run_id, "expires_at": _utcnow() + ttl}},
                session=self._session.get(),
            )
            return res.modified_count == 1

        async def release_idempotency_key(self, key, run_id) -> bool:
            self._assert_loop()
            res = await self._docs.IdempotencyKeyDoc.get_motor_collection().delete_one(
                {"key": key, "run_id": run_id}, session=self._session.get()
            )
            return res.deleted_count == 1

        # -- audit --
        async def append_audit(self, event: AuditEvent) -> None:
            self._assert_loop()
            await self._docs.AuditEventDoc(**self._audit_fields(event)).insert(
                session=self._session.get()
            )

        async def query_audit(self, query: AuditQuery) -> list[AuditEvent]:
            self._assert_loop()
            q = self._docs.AuditEventDoc.find(session=self._session.get())
            if query.run_id is not None:
                q = q.find(self._docs.AuditEventDoc.run_id == query.run_id)
            if query.instance_id is not None:
                q = q.find(self._docs.AuditEventDoc.instance_id == query.instance_id)
            if query.event_types is not None:
                codes = [e.value for e in query.event_types]
                q = q.find({"event_type": {"$in": codes}})
            if query.since is not None:
                q = q.find(self._docs.AuditEventDoc.at >= query.since)
            if query.until is not None:
                q = q.find(self._docs.AuditEventDoc.at <= query.until)
            docs = await q.sort([("run_id", 1), ("sequence", 1)]).skip(
                query.offset
            ).limit(query.limit).to_list()
            return [self._to_audit(d) for d in docs]

        async def next_audit_sequence(self, run_id: str) -> int:
            self._assert_loop()
            assert self._db is not None
            # No upsert on the hot path (see create_job_run): a plain $inc on an
            # existing document is not a write-conflict source inside transactions.
            res = await self._db["etf_counters"].find_one_and_update(
                {"_id": run_id},
                {"$inc": {"seq": 1}},
                return_document=pymongo.ReturnDocument.AFTER,
                session=self._session.get(),
            )
            if res is None:  # counter missing (pre-upgrade data) — rare slow path
                res = await self._db["etf_counters"].find_one_and_update(
                    {"_id": run_id},
                    {"$inc": {"seq": 1}},
                    upsert=True,
                    return_document=pymongo.ReturnDocument.AFTER,
                    session=self._session.get(),
                )
            return int(res["seq"])

        # ----- field mappers (domain -> doc dict) -----
        @staticmethod
        def _run_fields(run: JobRun) -> dict[str, Any]:
            return {
                "etf_id": run.id, "instance_id": run.instance_id, "job_name": run.job_name,
                "definition_version": run.definition_version,
                "definition_fingerprint": run.definition_fingerprint,
                "status": run.status.value, "exit_status": exit_to_dict(run.exit_status),
                "attempt": run.attempt, "restart_of": run.restart_of, "current_step": run.current_step,
                "parameters": params_to_dict(run.parameters), "execution_context": run.execution_context,
                "failures": [failure_to_dict(f) for f in run.failures],
                "open_validation_id": run.open_validation_id, "requested_by": actor_to_dict(run.requested_by),
                "correlation_id": run.correlation_id, "trace_id": run.trace_id,
                "idempotency_key": run.idempotency_key, "create_time": run.create_time,
                "start_time": run.start_time, "end_time": run.end_time, "last_updated": run.last_updated,
            }

        @staticmethod
        def _step_fields(s: StepRun) -> dict[str, Any]:
            return {
                "etf_id": s.id, "run_id": s.run_id, "instance_id": s.instance_id, "step_name": s.step_name,
                "status": s.status.value, "exit_status": exit_to_dict(s.exit_status), "attempt": s.attempt,
                "execution_context": s.execution_context, "read_count": s.read_count,
                "write_count": s.write_count, "skip_count": s.skip_count, "commit_count": s.commit_count,
                "attributes": dict(s.attributes),
                "failures": [failure_to_dict(f) for f in s.failures], "start_time": s.start_time,
                "end_time": s.end_time, "updated_at": s.updated_at,
            }

        @staticmethod
        def _vr_fields(v: ValidationRequest) -> dict[str, Any]:
            return {
                "etf_id": v.id, "run_id": v.run_id, "instance_id": v.instance_id, "step_name": v.step_name,
                "reason": v.reason, "payload": dict(v.payload), "decision_schema": dict(v.decision_schema),
                "required_role": v.required_role, "status": v.status.value, "created_at": v.created_at,
                "sla_deadline": v.sla_deadline, "decided_at": v.decided_at,
                "decision_type": v.decision_type.value if v.decision_type else None,
                "decision_actor_id": v.decision_actor_id,
                "decision_comment": v.decision_comment,
                "decision_overrides": dict(v.decision_overrides),
            }

        @staticmethod
        def _audit_fields(e: AuditEvent) -> dict[str, Any]:
            return {
                "etf_id": e.id, "event_type": e.event_type.value, "run_id": e.run_id,
                "instance_id": e.instance_id, "sequence": e.sequence, "at": e.at,
                "actor": actor_to_dict(e.actor), "step_name": e.step_name,
                "from_status": e.from_status.value if e.from_status else None,
                "to_status": e.to_status.value if e.to_status else None,
                "exit_status": exit_to_dict(e.exit_status), "failure": failure_to_dict(e.failure) if e.failure else None,
                "message": e.message, "attributes": dict(e.attributes),
                "correlation_id": e.correlation_id, "trace_id": e.trace_id,
            }

        # ----- doc -> domain -----
        @staticmethod
        def _to_instance(d: Any) -> JobInstance:
            return JobInstance(
                id=d.etf_id, job_name=d.job_name, identity_hash=d.identity_hash,
                definition_version=d.definition_version,
                identifying_parameters=dict(d.identifying_parameters), labels=dict(d.labels or {}),
                status=BatchStatus(d.status),
                created_at=_aware(d.created_at), updated_at=_aware(d.updated_at), version=d.version,  # type: ignore[arg-type]
            )

        @staticmethod
        def _to_run(d: Any) -> JobRun:
            return JobRun(
                id=d.etf_id, instance_id=d.instance_id, job_name=d.job_name,
                definition_version=d.definition_version,
                definition_fingerprint=d.definition_fingerprint,
                parameters=params_from_dict(d.parameters), status=BatchStatus(d.status),
                exit_status=exit_from_dict(d.exit_status), attempt=d.attempt, restart_of=d.restart_of,
                current_step=d.current_step, execution_context=d.execution_context,
                failures=[failure_from_dict(f) for f in d.failures], open_validation_id=d.open_validation_id,
                requested_by=actor_from_dict(d.requested_by), correlation_id=d.correlation_id,
                trace_id=d.trace_id, idempotency_key=d.idempotency_key, create_time=_aware(d.create_time),  # type: ignore[arg-type]
                start_time=_aware(d.start_time), end_time=_aware(d.end_time),
                last_updated=_aware(d.last_updated), version=d.version,  # type: ignore[arg-type]
            )

        @staticmethod
        def _to_step(d: Any) -> StepRun:
            return StepRun(
                id=d.etf_id, run_id=d.run_id, instance_id=d.instance_id, step_name=d.step_name,
                status=BatchStatus(d.status), exit_status=exit_from_dict(d.exit_status), attempt=d.attempt,
                execution_context=d.execution_context, read_count=d.read_count, write_count=d.write_count,
                skip_count=d.skip_count, commit_count=d.commit_count,
                attributes=dict(d.attributes),
                failures=[failure_from_dict(f) for f in d.failures], start_time=_aware(d.start_time),
                end_time=_aware(d.end_time), updated_at=_aware(d.updated_at), version=d.version,  # type: ignore[arg-type]
            )

        @staticmethod
        def _to_vr(d: Any) -> ValidationRequest:
            return ValidationRequest(
                id=d.etf_id, run_id=d.run_id, instance_id=d.instance_id, step_name=d.step_name,
                reason=d.reason, payload=dict(d.payload), decision_schema=dict(d.decision_schema),
                required_role=d.required_role, status=ValidationStatus(d.status),
                created_at=_aware(d.created_at), sla_deadline=_aware(d.sla_deadline),  # type: ignore[arg-type]
                decided_at=_aware(d.decided_at),
                decision_type=ValidationDecisionType(d.decision_type) if d.decision_type else None,
                decision_actor_id=d.decision_actor_id,
                decision_comment=d.decision_comment,
                decision_overrides=dict(d.decision_overrides),
                version=d.version,
            )

        @staticmethod
        def _to_audit(d: Any) -> AuditEvent:
            return AuditEvent(
                id=d.etf_id, event_type=AuditEventType(d.event_type), run_id=d.run_id,
                instance_id=d.instance_id, sequence=d.sequence, at=_aware(d.at),  # type: ignore[arg-type]
                actor=actor_from_dict(d.actor), step_name=d.step_name,
                from_status=BatchStatus(d.from_status) if d.from_status else None,
                to_status=BatchStatus(d.to_status) if d.to_status else None,
                exit_status=exit_from_dict(d.exit_status) if d.exit_status else None,
                failure=failure_from_dict(d.failure) if d.failure else None,
                message=d.message, attributes=dict(d.attributes),
                correlation_id=d.correlation_id, trace_id=d.trace_id,
            )

    # ------------------------------------------------------------------ #
    # Mongo TTL-leased lock provider (the default RunLockProvider)
    # ------------------------------------------------------------------ #
    from ..locking import Lock, RunLockProvider

    class _MongoLock(Lock):
        def __init__(
            self,
            coll: Any,
            key: str,
            owner: str,
            lease_id: str,
            fencing_token: int,
            loop: asyncio.AbstractEventLoop,
        ) -> None:
            self._coll, self._key, self._owner, self._lease_id = coll, key, owner, lease_id
            self._fencing_token = fencing_token
            self._loop = loop

        @property
        def fencing_token(self) -> int:
            return self._fencing_token

        def _assert_loop(self) -> None:
            if asyncio.get_running_loop() is not self._loop:
                raise RuntimeError(
                    "Mongo lock was initialized on a different event loop; use the "
                    "same AsyncBridge for initialization and every lock operation"
                )

        async def refresh(self, ttl: timedelta) -> bool:
            self._assert_loop()
            ttl_ms = max(1, int(ttl.total_seconds() * 1000))
            res = await self._coll.update_one(
                {
                    "_id": self._key,
                    "owner": self._owner,
                    "lease_id": self._lease_id,
                    "$expr": {"$gt": ["$expires_at", "$$NOW"]},
                },
                [
                    {
                        "$set": {
                            "expires_at": {
                                "$dateAdd": {
                                    "startDate": "$$NOW",
                                    "unit": "millisecond",
                                    "amount": ttl_ms,
                                }
                            }
                        }
                    }
                ],
            )
            return res.modified_count == 1

        async def release(self) -> None:
            self._assert_loop()
            await self._coll.delete_one(
                {"_id": self._key, "owner": self._owner, "lease_id": self._lease_id}
            )

    class MongoLockProvider(RunLockProvider):
        """Single-writer locks via a TTL-leased Mongo collection. A crashed worker's lease
        auto-expires (server-side TTL index) so the instance becomes reclaimable."""

        def __init__(self, db: Any, collection: str = "etf_locks") -> None:
            self._coll = db[collection]
            self._counters = db[f"{collection}_fencing"]
            self._loop: asyncio.AbstractEventLoop | None = None

        async def initialize(self) -> None:
            running_loop = asyncio.get_running_loop()
            if self._loop is not None and self._loop is not running_loop:
                raise RuntimeError(
                    "MongoLockProvider was already initialized on a different event loop"
                )
            self._loop = running_loop
            await self._coll.create_index("expires_at", expireAfterSeconds=0)

        async def acquire(self, key: str, owner: str, ttl: timedelta) -> Lock | None:
            if self._loop is None:
                raise RuntimeError("MongoLockProvider.initialize() must be awaited first")
            if asyncio.get_running_loop() is not self._loop:
                raise RuntimeError(
                    "MongoLockProvider was initialized on a different event loop; "
                    "use the same AsyncBridge for every lock operation"
                )
            lease_id = uuid.uuid4().hex
            counter = await self._counters.find_one_and_update(
                {"_id": key},
                {"$inc": {"value": 1}},
                upsert=True,
                return_document=pymongo.ReturnDocument.AFTER,
            )
            fencing_token = int(counter["value"])
            ttl_ms = max(1, int(ttl.total_seconds() * 1000))
            lease = [
                {
                    "$set": {
                        "owner": owner,
                        "lease_id": lease_id,
                        "fencing_token": fencing_token,
                        "expires_at": {
                            "$dateAdd": {
                                "startDate": "$$NOW",
                                "unit": "millisecond",
                                "amount": ttl_ms,
                            }
                        },
                    }
                }
            ]
            # MongoDB rejects $expr in an upsert's query predicate, so the server-clock
            # expiry check and the create are two atomic operations. First steal an
            # existing lease that has expired (by the server's clock) in place.
            acquired = await self._coll.find_one_and_update(
                {
                    "_id": key,
                    "$or": [
                        {"expires_at": {"$exists": False}},
                        {"$expr": {"$lte": ["$expires_at", "$$NOW"]}},
                    ],
                },
                lease,
                return_document=pymongo.ReturnDocument.AFTER,
            )
            if acquired is None:
                # No expired lease to steal: create one. Every lease has expires_at, so
                # this filter never matches an existing row. A live lease makes the
                # upsert hit the duplicate _id and the contender loses, without relying
                # on either worker's wall clock.
                try:
                    acquired = await self._coll.find_one_and_update(
                        {"_id": key, "expires_at": {"$exists": False}},
                        lease,
                        upsert=True,
                        return_document=pymongo.ReturnDocument.AFTER,
                    )
                except DuplicateKeyError:
                    return None
            if acquired is None:
                return None
            return _MongoLock(
                self._coll,
                key,
                owner,
                lease_id,
                fencing_token,
                self._loop,
            )

    __all__ = ["BeanieStateStore", "MongoLockProvider"]

else:  # pragma: no cover - import-guard branch

    def __getattr__(name: str):
        if name in {"BeanieStateStore", "MongoLockProvider"}:
            raise ImportError(
                "BeanieStateStore requires the 'mongo' extra. Install with: "
                "pip install 'enhanced-task-framework[mongo]'"
            ) from _IMPORT_ERROR
        raise AttributeError(name)
