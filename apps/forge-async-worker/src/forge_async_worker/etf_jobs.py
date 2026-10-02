"""Every job the worker runs goes through the enhanced task framework (ETF).

Each (task type, job kind) is an ETF job named ``<task>.<kind>``
(``commits.backfill``) with one step that runs the task's job — the same code the
inline runner runs, follow-ups and observer included. So every delivery is a
tracked, audited run: its status, failures and result are stored, a crashed
worker's run is recovered and re-driven, and operators can stop, restart or
decide it through ``JobOperator``.

A SAQ delivery is one ETF instance, identified by its queue, key and enqueue
time: SAQ's retries of a delivery continue that run's history, while every cron
tick and every re-submission is a run of its own. The instance is labelled with
its task type, job kind and organization (``tenant``, when the work is an
organization's), which is how an organization's background tasks are listed
(api.py).

The job runs under a control (job_control.py, forge_tasks.control) backed by
its run: what it keeps between attempts, checkpoints, approvals (the task
framework's human-in-the-loop gates), waits until a time (the run stops and
a SAQ job resumes it then), notes on its activity, and other runs by label.
Most jobs never use it; an ADK workflow run lives on it.

Retries stay SAQ's: the step makes one attempt. A job that raises a
:class:`~forge_tasks.errors.TransientError` fails its run with a *retryable*
failure; the worker hands the delivery back to SAQ, which retries it with
backoff (freeing the slot meanwhile), and the redelivery restarts the run as its
next attempt, skipping nothing it did not finish.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from etf import (
    Actor,
    BackoffRetryPolicy,
    BatchStatus,
    EtfConfig,
    FailureRecord,
    JobDefinition,
    JobInstanceAlreadyCompleteError,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    JobRun,
    LaunchRequest,
    StateStore,
    Step,
    StepContext,
    StepResult,
)
from etf.audit import StoreBackedAuditSink
from forge_async_worker.job_control import WAITING_KEY, EtfJobControl
from forge_tasks.control import controlling
from forge_tasks.errors import TaskError
from forge_tasks.tasks import JobResult, JobSpec, JobStatus

if TYPE_CHECKING:
    from forge_async_worker.config import WorkerSettings
    from forge_tasks.protocols import Task
    from forge_tasks.runner import JobRunner

log = logging.getLogger(__name__)

JOB_STEP = "run"
REQUESTER = Actor.service("forge-async-worker")
# The worker's own labels on a run: a job's labels never replace them.
OWN_LABELS = frozenset({"task_type", "kind", "tenant"})


class JobFailed(TaskError):
    """The job returned a FAILED result: a permanent failure, recorded on its run."""

    permanent = True


def job_name(spec: JobSpec) -> str:
    return f"{spec.task_type}.{spec.kind}"


def classify_failure(exc: BaseException, record: FailureRecord) -> None:
    """
    Classifies a given exception into retryable or non-retryable failure categories
    and updates the specified failure record accordingly.

    The function evaluates the provided exception to determine if it is of type
    TaskError. If so, it modifies the `retryable` status of the given failure
    record and updates its attributes with additional information regarding the
    nature of the TaskError. The modifications are applied directly to the
    `record` object.

    :param exc: The exception to classify. It determines whether the failure
        is retryable.
    :type exc: BaseException
    :param record: The failure record that holds information about the exception
        classification, including retryability and any associated attributes.
        This object is modified in place.
    :type record: FailureRecord
    :return: None
    """
    if isinstance(exc, TaskError):
        record.retryable = not exc.permanent
        record.attributes["permanent"] = exc.permanent


class TaskJobStep(Step):
    """
    Represents a step in a task job execution pipeline.

    This class is responsible for managing the execution of a specific job step
    by using the provided job runner, task type, and kind. It utilizes a job
    specification and interacts with the job control system to manage job
    execution and result handling. This class is designed for asynchronous
    operation and integrates with the step execution context.

    :ivar name: Constant identifier for this type of step, set to `JOB_STEP`.
    :type name: str
    """

    name = JOB_STEP

    def __init__(self, runner: JobRunner, task_type: str, kind: str, store: StateStore | None = None) -> None:
        self.runner = runner
        self.task_type = task_type
        self.kind = kind
        self.store = store

    async def execute(self, ctx: StepContext) -> StepResult:
        """
        Executes a specific step within a defined context, processes a job and
        returns the result. This function interacts with job controls, retrieves
        task specifications, handles follow-up actions, and ensures error handling
        when jobs fail.

        :param ctx: The step context in which this execution occurs. Includes the
            parameters and state necessary for processing the task.
        :type ctx: StepContext
        :return: An instance of StepResult which contains the outcome of the job
            execution, including any attributes returned from the executed job.
        :rtype: StepResult
        :raises JobFailed: If the job execution fails, this exception is raised
            with an error message describing the failure.
        """
        payload = ctx.params.get("payload") or {}
        spec = JobSpec(task_type=self.task_type, kind=self.kind, payload=thaw(payload))  # type: ignore[arg-type]
        if ctx.get(WAITING_KEY):
            ctx.put(WAITING_KEY, None)  # resumed: it's running again, not waiting
        control = EtfJobControl(ctx, task_type=self.task_type, queue=self.runner.queue, store=self.store)
        with controlling(control):
            result = await self.runner.run(spec)  # follow-ups are enqueued before the step completes
        if result.status is JobStatus.FAILED:
            raise JobFailed(result.error or f"{spec.label()} failed")
        return StepResult(attributes={"result": result.model_dump(mode="json")})


def build_job_registry(tasks: Mapping[str, Task], runner: JobRunner, store: StateStore | None = None) -> JobRegistry:
    """
    Builds and returns a job registry that maps task definitions and their corresponding job
    steps for execution. This function iterates through a mapping of tasks, constructs a
    registry of job definitions, and registers them with the provided runner for execution.

    :param tasks: A mapping where the key is a string representing the task name and the
        value is a Task object containing job definitions mapped to specific task kinds.
    :param runner: The job runner responsible for executing the defined tasks' jobs.
    :param store: Optional; A state store to persist or manage states during the execution
        of the job steps.
    :return: An instance of JobRegistry containing all the registered job definitions.
    """
    registry = JobRegistry()
    for task_name, task in tasks.items():
        for kind in task.jobs:
            registry.register(
                JobDefinition(name=f"{task_name}.{kind}", steps=[TaskJobStep(runner, task_name, kind, store)])
            )
    return registry


def launch_request(
    spec: JobSpec, *, queue: str, key: str, enqueued: int, description: str | None = None
) -> LaunchRequest:
    """
    Constructs and returns a LaunchRequest instance, which represents the details needed to
    launch a job with specific attributes, parameters, and metadata. This function processes
    the provided `spec` object and uses it to populate identifying and non-identifying
    parameters for the request.

    :param spec: The job specification containing details about the job, such as task type,
        kind, payload, and labels.
    :type spec: JobSpec
    :param queue: The name of the queue to which the job belongs.
    :type queue: str
    :param key: A unique key associated with the job within the specified queue.
    :type key: str
    :param enqueued: A timestamp or sequence number that indicates when the job was enqueued.
    :type enqueued: int
    :param description: An optional string describing the job or providing additional
        context about it.
    :type description: str | None
    :return: A LaunchRequest object populated with job details including parameters, labels,
        and metadata derived from the input arguments.
    :rtype: LaunchRequest
    """
    delivery = f"{queue}:{key}"
    labels = {name: value for name, value in spec.labels.items() if name not in OWN_LABELS}
    labels.update({"task_type": spec.task_type, "kind": spec.kind})
    non_identifying: dict[str, Any] = {"payload": spec.payload}
    if spec.labels:
        non_identifying["labels"] = dict(spec.labels)  # so a resubmission carries them
    if spec.tenant is not None:
        labels["tenant"] = non_identifying["tenant_id"] = spec.tenant
    if description:
        non_identifying["description"] = description
    return LaunchRequest(
        job_name=job_name(spec),
        parameters=JobParameters(
            identifying={"delivery": delivery, "enqueued": enqueued},
            non_identifying=non_identifying,
        ),
        idempotency_key=f"{delivery}@{enqueued}",
        requested_by=_requester(spec),
        correlation_id=delivery,
        source="saq",
        labels=labels,
    )


def _requester(spec: JobSpec) -> Actor:
    """
    Determines and returns the appropriate requester (Actor) based on the
    provided spec. This could either be a human Actor derived from the
    `requested_by` information in the given spec or a default predefined
    Actor (`REQUESTER`).

    :param spec: Job specification containing details about the requesting
                 entity.
    :type spec: JobSpec
    :return: An Actor instance representing the requester, either derived from
             the `requested_by` field of the provided spec or a default
             requester.
    :rtype: Actor
    """
    person = spec.requested_by or {}
    if person.get("id"):
        return Actor.human(str(person["id"]), str(person.get("display_name") or ""))
    return REQUESTER


def thaw(value: Any) -> Any:
    """
    Thaws a deeply nested Python object by serializing it into JSON string format
    and deserializing back into the original Python object. This ensures that
    mutable nested structures are re-created, breaking any shared references.

    :param value: The input Python object to be thawed. It can be any nested Python
      data structure like dictionaries, lists, or tuples.
    :return: A new Python object, equivalent to the input `value`, but with
      shared references in nested structures broken.
    """
    return json.loads(json.dumps(value))


def spec_of(run: JobRun) -> JobSpec:
    """
    Generates a `JobSpec` object based on the provided `JobRun` instance by extracting and processing relevant
    information from its attributes.

    :param run: The `JobRun` object containing details of a job execution such as the job name, parameters,
    and associated data.
    :type run: JobRun

    :return: A `JobSpec` object encapsulating task type, kind, tenant ID, payload, and labels extracted
    and processed from the provided `JobRun` instance.
    :rtype: JobSpec
    """
    task_type, kind = run.job_name.split(".", 1)
    payload = run.parameters.get("payload")
    tenant = run.parameters.get("tenant_id")
    labels = run.parameters.get("labels")
    return JobSpec(
        task_type=task_type,
        kind=kind,
        payload=thaw(payload) if isinstance(payload, dict) else {},
        tenant_id=tenant if isinstance(tenant, str) else None,
        labels={str(k): str(v) for k, v in thaw(labels).items()} if isinstance(labels, dict) else {},
    )


@dataclass
class TaskRuns:
    """
    Represents a set of task runs capable of managing job launching, retrieving job results,
    and handling the closure of resources.

    Provides the core functionality to launch job runs, track their results, and interact with
    the underlying state store. This class encapsulates the workflow of managing durable
    state transitions for deliveries.

    :ivar config: Configuration details required for task execution.
    :type config: EtfConfig
    :ivar launcher: Responsible for launching job runs.
    :type launcher: JobLauncher
    :ivar operator: Handles job operation tasks.
    :type operator: JobOperator
    :ivar store: Manages state storage for jobs.
    :type store: StateStore
    """

    config: EtfConfig
    launcher: JobLauncher
    operator: JobOperator
    store: StateStore

    async def launch(
        self, spec: JobSpec, *, queue: str, key: str, enqueued: int, description: str | None = None
    ) -> JobRun:
        """
        Launches a job asynchronously with the provided specifications and parameters.

        This method interacts with the launcher and job storage to initiate a new job
        run based on the provided `JobSpec`. If the job instance is already complete,
        the method attempts to locate the latest run of the job and returns it.

        :param spec: The job specification containing the details needed to execute
            the job.
        :type spec: JobSpec
        :param queue: The name of the job queue where the job is to be enqueued.
        :type queue: str
        :param key: Unique key identifying the job instance.
        :type key: str
        :param enqueued: The timestamp indicating when the job was enqueued, in Unix
            time.
        :type enqueued: int
        :param description: Optional description of the job. Defaults to None.
        :type description: str | None
        :return: The result of the job run if successfully initiated or located.
        :rtype: JobRun
        :raises JobInstanceAlreadyCompleteError: If the job instance is already marked
            complete and no previous run can be located.
        """
        request = launch_request(spec, queue=queue, key=key, enqueued=enqueued, description=description)
        try:
            return await self.launcher.launch(request)
        except JobInstanceAlreadyCompleteError:
            instance = await self.store.find_job_instance(
                request.job_name,
                self.config.instance_resolver.resolve_identity(request.job_name, request.parameters),
            )
            latest = await self.store.find_latest_run(instance.id) if instance is not None else None
            if latest is None:
                raise
            return latest

    async def result(self, run: JobRun) -> JobResult:
        """
        Determines the result of a job run by evaluating the stored result of its step runs.

        For each step associated with the provided job run, it checks if the step is
        the specific "JOB_STEP" type and whether it contains a valid result stored in
        its attributes. If such a result exists, the method validates it using the
        `JobResult` model and assigns it as the final result. Otherwise, a default
        result of status `JobStatus.OK` is used. The `run_id` of the job run is
        included in the details of the result before returning.

        :param run: The job run whose result is to be determined.
        :type run: JobRun
        :return: The finalized job result containing the aggregated outcome of the
                 evaluated step results.
        :rtype: JobResult
        """
        result = JobResult(status=JobStatus.OK)
        for step in await self.store.find_step_runs(run.id):
            stored = step.attributes.get("result") if step.step_name == JOB_STEP else None
            if isinstance(stored, dict):
                result = JobResult.model_validate(stored)
                break
        result.detail.setdefault("run_id", run.id)
        return result

    async def close(self) -> None:
        await self.store.close()


async def start_task_runs(
    settings: WorkerSettings,
    tasks: Mapping[str, Task] | None,
    runner: JobRunner | None,
    *,
    max_auto_redrives: int,
    **config: Any,
) -> TaskRuns:
    """
    Starts and initializes task runs within a configured environment. Based on the provided
    settings and task definitions, it determines the type of data store and locking mechanism
    to use. Configures job registry, retry policies, failure classification, and initializes
    a launcher, operator, and store for task runs.

    :param settings: Configuration object defining worker settings, including database and
                     other operational details.
    :type settings: WorkerSettings
    :param tasks: A mapping of task names to corresponding Task objects. If None, it creates
                  an empty job registry.
    :type tasks: Mapping[str, Task] | None
    :param runner: An instance of JobRunner used for executing tasks. If None, it creates
                   an empty job registry.
    :type runner: JobRunner | None
    :param max_auto_redrives: The maximum number of automatic retries allowed for failed
                              jobs or tasks.
    :type max_auto_redrives: int
    :param config: Arbitrary keyword arguments passed for additional configurations.
    :type config: Any
    :return: A TaskRuns object containing the configured environment, launcher, operator,
             and store for task execution.
    :rtype: TaskRuns
    """
    store: StateStore
    if settings.etf.store == "memory":
        from etf.locking import InMemoryLockProvider
        from etf.stores.memory import InMemoryStateStore

        store = InMemoryStateStore()
        await store.initialize()
        locks: Any = InMemoryLockProvider()
    else:
        from motor.motor_asyncio import AsyncIOMotorClient

        from etf.stores.beanie_store import BeanieStateStore, MongoLockProvider

        uri = settings.mongo.uri.get_secret_value()
        client: Any = AsyncIOMotorClient(uri, appname="forge-async-worker", tz_aware=True)
        store = BeanieStateStore(uri, db_name=settings.etf.database, client=client)
        await store.initialize()
        locks = MongoLockProvider(client[settings.etf.database])
        await locks.initialize()
    etf_config = EtfConfig(
        store=store,
        audit=StoreBackedAuditSink(store),
        lock_provider=locks,
        registry=build_job_registry(tasks, runner, store)
        if tasks is not None and runner is not None
        else JobRegistry(),
        retry_policy=BackoffRetryPolicy(max_attempts=1),  # SAQ retries, with backoff
        max_auto_redrives=max_auto_redrives,
        failure_classifier=classify_failure,
        **config,
    )
    return TaskRuns(
        config=etf_config,
        launcher=JobLauncher(etf_config),
        operator=JobOperator(etf_config),
        store=store,
    )


def is_settled(run: JobRun) -> bool:
    """
    Determines whether the given job run is in a settled state.

    A job run is considered settled if its status is one of the predefined states
    indicating the completion or suspension of the job. These states include
    `COMPLETED`, `ABANDONED`, `AWAITING_VALIDATION`, `PAUSED`, or `STOPPED`.

    :param run: The job run instance to evaluate.
    :type run: JobRun
    :return: True if the job run is in a settled state; otherwise, False.
    :rtype: bool
    """
    return run.status in (
        BatchStatus.COMPLETED,
        BatchStatus.ABANDONED,
        BatchStatus.AWAITING_VALIDATION,
        BatchStatus.PAUSED,
        BatchStatus.STOPPED,
    )
