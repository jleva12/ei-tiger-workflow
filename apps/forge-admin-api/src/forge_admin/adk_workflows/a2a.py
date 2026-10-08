"""
The organizations' workflows over Google's A2A protocol (JSON-RPC, 1.0 and
0.3), at the chat agents' URLs: ``{api_prefix}/runtime/a2a/{ref}`` and its
card, ``{ref}`` a workflow's (``ag_x``, ``ag_x@3``, ``ag_x@draft``;
``versions.py``, outside rules). Who may call is the runtime's rule, as for
its REST API (``runtime.py``).

A task is a run, the task's ID the run's (as hex):

- The first message starts it: its input is the message's data part, or its
  text as JSON, or its text. Its file parts (their bytes: a file's URL
  isn't fetched) are the files the run starts with, when its start takes
  them. Input or files that don't fit the start are rejected.
- The task follows the run for up to ``workflow_runtime_wait`` seconds:
  ``working`` while it goes; ``input-required`` when it waits for someone (a
  question, with what the answer must fit; an approval or a tool call to
  confirm, with who decides); ``completed`` with its result as the artifact
  ``result``; ``failed``; ``canceled`` once given up.
- The next message on the task answers what it waits at: a JSON object (or
  data part) for a question; ``approve`` or ``reject`` (or
  ``{"approved": true, "comment": …}``) for an approval, which only the
  organization's members and keys holding what the step asks may decide.
- Getting the task (``GetTask``) catches it up with its run, which may have
  moved on since (decided in the console, or finished after the wait).
- Cancelling it gives the run up, for its starter or those who manage runs.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from a2a.helpers import get_data_parts, get_text_parts, new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import Event, EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Part,
    SecurityRequirement,
    StringList,
    SubscribeToTaskRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_0_3, PROTOCOL_VERSION_1_0
from a2a.utils.errors import (
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from a2a.utils.task import apply_history_length
from fastapi import HTTPException, Request
from forge_agent_runtime.a2a import (
    APP_KEY,
    REQUEST_KEY,
    A2aService,
    ForgeRequestHandler,
    agent_message,
)
from forge_agent_runtime.server import CallAction
from forge_task_adk_workflows.files import FILE_TYPES, files_rule
from forge_task_adk_workflows.run_store import ABANDONABLE, PAUSED
from google.protobuf.struct_pb2 import Struct

from forge_admin.adk_workflows.files import Limits, SentFile
from forge_admin.adk_workflows.runs import GOING, start_schema
from forge_admin.adk_workflows.runtime import (
    MANAGE_RUNS,
    answer_run,
    caller_actor,
    decide_run,
    found_workflow,
    may,
    runtime_pause,
    start_runtime_run,
    wait_of,
)
from forge_admin.adk_workflows.versions import ResolvedWorkflow
from forge_admin.api.routes.adk_workflow_runs import HUMAN_INPUT, run_store
from forge_admin.auth.runtime_access import caller_of
from forge_admin.config import Settings

logger = logging.getLogger(__name__)

PREFIX = "ag_"
#: Says where in a card the input a workflow's start takes is.
INPUT_EXTENSION = "https://forge.dev/a2a/workflow-input/v1"
#: The most characters of a start's schema a skill's description quotes.
SCHEMA_IN_DESCRIPTION = 4000
TERMINAL = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
}
YES = {"approve", "approved", "yes", "y", "ok", "okay", "confirm", "go"}
NO = {"reject", "rejected", "no", "n", "deny", "denied", "cancel", "stop"}


def run_id_of(task_id: str) -> str:
    """:return: The run a task is: its ID, as the run store's hex."""
    try:
        return uuid.UUID(task_id).hex
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, task_id).hex


def _compact(schema: dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def workflow_card(
    resolved: ResolvedWorkflow, url: str, *, security: dict[str, Any] | None
) -> AgentCard:
    """
    A workflow's A2A card: its name, what it does, and the input its start
    takes (an extension's ``input_schema``, and in its skill's description).
    """
    document = resolved.document
    workflow_id = resolved.record["_id"]
    name = str(document.get("name") or workflow_id)
    about = str(document.get("description") or "") or f"Runs the workflow {name}."
    schema = start_schema(document) or {}
    quoted = _compact(schema)
    if len(quoted) > SCHEMA_IN_DESCRIPTION:
        quoted = quoted[:SCHEMA_IN_DESCRIPTION] + "…"
    rule = files_rule(document)
    params = Struct()
    params.update(
        {
            "input_schema": schema,
            "files": {"allowed": rule.allowed, "types": list(rule.types)},
        }
    )
    # What a task's first message may send: its input, and files of the
    # types the start takes (any, when it names none).
    accepted = ["application/json", "text/plain"]
    if rule.allowed:
        media = sorted(
            {
                m
                for t in rule.types
                if t in FILE_TYPES
                for m in FILE_TYPES[t].extensions.values()
            }
        )
        accepted += [m for m in media if m not in accepted] if media else ["*/*"]
    files_said = (
        f"\n\nSend its files as file parts ({rule.describe()}), with their bytes."
        if rule.allowed
        else ""
    )
    card = AgentCard(
        name=name,
        description=about,
        version=str(resolved.version),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
            AgentInterface(
                url=url,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_0_3,
            ),
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extensions=[
                AgentExtension(
                    uri=INPUT_EXTENSION,
                    description="input_schema: the JSON Schema of what a task's "
                    "first message sends (as a data part, or JSON text); files: "
                    "whether it may send files (as file parts with their bytes), "
                    "and their types (none: any)",
                    params=params,
                )
            ],
        ),
        default_input_modes=accepted,
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id=workflow_id,
                name=name,
                description=f"{about}\n\nSend its input as JSON fitting: {quoted}{files_said}",
                tags=["workflow"],
                input_modes=accepted,
                output_modes=["application/json"],
            )
        ],
    )
    for scheme_name, scheme in (security or {}).items():
        card.security_schemes[scheme_name].CopyFrom(scheme)
        card.security_requirements.append(
            SecurityRequirement(schemes={scheme_name: StringList()})
        )
    return card


def value_of(message: Message | None) -> Any:
    """:return: What a message sends: its data part, else its text as JSON,
    else its text; nothing when it only sends files."""
    if message is None:
        return None
    data = get_data_parts(message.parts)
    if data:
        return data[0]
    text = "\n".join(get_text_parts(message.parts)).strip()
    if not text and any(_is_file(part) for part in message.parts):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _is_file(part: Part) -> bool:
    return part.WhichOneof("content") in ("raw", "url")


def files_of(message: Message, limits: Limits) -> list[SentFile]:
    """
    :return: The files a message sends: its file parts' bytes, names and media types.
    :raises HTTPException: 422 for a file sent by URL (never fetched) or too
        many files; 413 for one too big.
    """
    sent: list[SentFile] = []
    for part in message.parts:
        kind = part.WhichOneof("content")
        if kind == "url":
            raise HTTPException(
                422,
                f"{part.filename or part.url}: send a file's bytes, not its URL: "
                "the workflow doesn't fetch files",
            )
        if kind == "raw":
            limits.check_size(part.filename or None, len(part.raw))
            sent.append(
                SentFile(part.filename or None, part.media_type or None, part.raw)
            )
    limits.check_count(len(sent))
    return sent


def _going_text(run: dict[str, Any]) -> str:
    if run["status"] == "waiting":
        until = run.get("waiting_until")
        when = until.isoformat() if until is not None else "later"
        return f"Waiting until {when}: {run.get('waiting_reason') or 'a timer'}"
    return "Queued" if run["status"] == "queued" else "Running"


def update_of(run: dict[str, Any]) -> tuple[TaskState, Message, Artifact | None]:
    """
    :return: What a run says as a task: its state, the message saying so,
        and its result (the artifact ``result``) once it succeeded.
    """
    status = run["status"]
    if status == "succeeded":
        result = run.get("result")
        parts = [new_data_part(result)] if result is not None else []
        if isinstance(result, str):
            parts.insert(0, Part(text=result))
        artifact = (
            Artifact(artifact_id=f"{run['id']}-result", name="result", parts=parts)
            if parts
            else None
        )
        return TaskState.TASK_STATE_COMPLETED, agent_message("Done"), artifact
    if status == "failed":
        error = run.get("error") or {}
        text = str(error.get("message") or "The run failed")
        return TaskState.TASK_STATE_FAILED, agent_message(text), None
    if status == "abandoned":
        return TaskState.TASK_STATE_CANCELED, agent_message("Given up"), None
    if status == PAUSED:
        pause = runtime_pause(run)
        if pause is not None:
            return TaskState.TASK_STATE_INPUT_REQUIRED, _asking(pause), None
    return TaskState.TASK_STATE_WORKING, agent_message(_going_text(run)), None


def _sentence(text: str) -> str:
    """:return: The text as a sentence: ending in a stop, a question or an exclamation."""
    text = text.strip()
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _asking(pause: Any) -> Message:
    """:return: What a run waiting for someone asks: how to answer, and the details as data."""
    if pause.kind == HUMAN_INPUT:
        schema = pause.response_schema or {}
        text = (
            f"{_sentence(pause.reason)} Reply with JSON"
            + (f" fitting {_compact(schema)}" if schema else "")
            + "."
        )
        data = {
            "kind": HUMAN_INPUT,
            "request_id": pause.id,
            "step": pause.step,
            "response_schema": schema,
        }
    else:
        who = {"org:admin": "the organization's admins", "org:member": "its members"}
        decides = who.get(str(pause.approvers), "the organization's admins")
        what = f"Confirm calling {pause.tool}" if pause.confirmation else pause.reason
        text = (
            f"{_sentence(what)} Reply approve or reject (or "
            '{"approved": true, "comment": "…"}); '
            f"decided by {decides}, signed in or with an API key."
        )
        data = {
            "kind": "approval",
            "request_id": pause.id,
            "step": pause.step,
            "confirmation": pause.confirmation,
            "tool": pause.tool,
            "args": pause.args,
            "approvers": pause.approvers,
            "deadline": pause.deadline,
        }
    return agent_message(text, new_data_part(data))


async def report(
    updater: TaskUpdater, event_queue: EventQueue, run: dict[str, Any]
) -> None:
    """Say what the run is as the task's status (and result)."""
    state, message, artifact = update_of(run)
    if artifact is not None:
        await updater.add_artifact(
            list(artifact.parts), artifact_id=artifact.artifact_id, name=artifact.name
        )
    await updater.update_status(state, message=message)


class WorkflowA2aExecutor(AgentExecutor):
    """Runs A2A tasks as runs of the organizations' workflows (see the module)."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message, task_id, context_id = (
            context.message,
            context.task_id,
            context.context_id,
        )
        if message is None or not task_id or not context_id:
            raise ValueError("An A2A task needs a message, a task ID and a context ID")
        call = context.call_context
        request: Request = call.state[REQUEST_KEY]
        updater = TaskUpdater(event_queue, task_id, context_id)
        run_id = run_id_of(task_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_SUBMITTED,
                    history=[message],
                )
            )
            started = await self._start(
                request, call, updater, message, run_id, context_id
            )
            if not started:
                return
        else:
            run = await run_store(request).get(run_id)
            if run is None:
                await updater.failed(agent_message("The task's run is gone"))
                return
            if run["status"] == PAUSED and not await self._answer(
                request, updater, run, message
            ):
                return
        await self._follow(request, updater, event_queue, run_id)

    async def _start(
        self,
        request: Request,
        call: ServerCallContext,
        updater: TaskUpdater,
        message: Message,
        run_id: str,
        context_id: str,
    ) -> bool:
        caller = caller_of(request)
        resolved: ResolvedWorkflow = request.state.forge_workflow
        try:
            run = await start_runtime_run(
                request,
                caller,
                resolved,
                value_of(message),
                protocol="a2a",
                run_id=run_id,
                trigger={"task_id": updater.task_id, "context_id": context_id},
                files=files_of(message, Limits.of(request.app.state.settings)),
            )
        except HTTPException as error:
            text = str(error.detail)
            if error.status_code in (413, 422):
                await updater.reject(agent_message(text))
            else:
                await updater.failed(agent_message(text))
            return False
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=agent_message("Queued"),
            metadata={
                "forge": {
                    "run_id": run["id"],
                    "workflow": resolved.record["_id"],
                    "ref": resolved.ref,
                    "version": resolved.version,
                    "app": str(call.state.get(APP_KEY) or ""),
                }
            },
        )
        return True

    async def _answer(
        self,
        request: Request,
        updater: TaskUpdater,
        run: dict[str, Any],
        message: Message,
    ) -> bool:
        """
        Answer what the run waits at with the message. Not taken: the task
        asks again, saying why.

        :return: Whether it was taken.
        """
        pause = runtime_pause(run)
        assert pause is not None
        caller = caller_of(request)
        value = value_of(message)
        named = value.get("request_id") if isinstance(value, dict) else None
        request_id = str(named or pause.id)
        try:
            if pause.kind == HUMAN_INPUT:
                if (
                    isinstance(value, dict)
                    and "request_id" in value
                    and "answer" in value
                ):
                    value = value["answer"]
                await answer_run(request, caller, run, request_id, value)
            else:
                approved, comment = _decision(value)
                if approved is None:
                    await updater.requires_input(
                        agent_message(
                            "Reply approve or reject.", *_asking(pause).parts[1:]
                        )
                    )
                    return False
                await decide_run(request, caller, run, request_id, approved, comment)
        except HTTPException as error:
            await updater.requires_input(
                agent_message(f"{error.detail}.", *_asking(pause).parts[1:])
            )
            return False
        await updater.update_status(
            TaskState.TASK_STATE_WORKING, message=agent_message("Carrying on")
        )
        return True

    async def _follow(
        self,
        request: Request,
        updater: TaskUpdater,
        event_queue: EventQueue,
        run_id: str,
    ) -> None:
        """Follow the run until it waits for someone or ends, or the wait runs out."""
        settings: Settings = request.app.state.settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_of(settings, settings.workflow_runtime_wait)
        store = run_store(request)
        last: str | None = "queued"
        delay = 0.5
        while True:
            run = await store.get(run_id)
            if run is None:
                await updater.failed(agent_message("The task's run is gone"))
                return
            if run["status"] not in GOING:
                await report(updater, event_queue, run)
                return
            if loop.time() >= deadline:
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=agent_message(
                        f"{_going_text(run)}: get the task again to see how it ends"
                    ),
                )
                return
            if run["status"] != last:
                last = run["status"]
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=agent_message(_going_text(run)),
                )
            await asyncio.sleep(min(delay, max(0.0, deadline - loop.time())))
            delay = min(delay * 1.5, 2.0)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("Cancelling needs the task's ID")
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def _decision(value: Any) -> tuple[bool | None, str]:
    """:return: Whether a reply approves (None: it doesn't say), and why."""
    if isinstance(value, dict) and isinstance(value.get("approved"), bool):
        return value["approved"], str(value.get("comment") or "")
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, str):
        word, _, rest = value.strip().partition(" ")
        word = word.lower().rstrip(".!,:")
        if word in YES:
            return True, rest.strip()
        if word in NO:
            return False, rest.strip()
    return None, ""


class WorkflowRequestHandler(ForgeRequestHandler):
    """
    A2A's request handler for workflows: a task catches up with its run when
    it's read, and cancelling it gives the run up.
    """

    async def _synced(self, task: Task, context: ServerCallContext) -> Task:
        """:return: The task as its run is now, saved when that changed it."""
        if task.status.state in TERMINAL:
            return task
        request: Request = context.state[REQUEST_KEY]
        run = await run_store(request).get(run_id_of(task.id))
        if run is None:
            return task
        state, message, artifact = update_of(run)
        if state == task.status.state and state != TaskState.TASK_STATE_INPUT_REQUIRED:
            return task
        if (
            state == task.status.state
            and task.status.message.parts[-1:] == message.parts[-1:]
        ):
            return task
        status = TaskStatus(state=state)
        status.message.CopyFrom(message)
        status.timestamp.GetCurrentTime()
        task.status.CopyFrom(status)
        if artifact is not None and all(
            a.artifact_id != artifact.artifact_id for a in task.artifacts
        ):
            task.artifacts.append(artifact)
        await self.task_store.save(task, context)
        return task

    async def on_get_task(
        self, params: GetTaskRequest, context: ServerCallContext
    ) -> Task | None:
        found = await super().on_get_task(params, context)
        if found is None or found.status.state in TERMINAL:
            return found
        # The whole task (the handler's answer is trimmed to the history asked for).
        whole = await self.task_store.get(params.id, context)
        if whole is None:
            return found
        return apply_history_length(await self._synced(whole, context), params)

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        page = await super().on_list_tasks(params, context)
        for task in page.tasks:
            if task.status.state in TERMINAL:
                continue
            whole = await self.task_store.get(task.id, context)
            if whole is not None:
                synced = await self._synced(whole, context)
                task.status.CopyFrom(synced.status)
        return page

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        request: Request = context.state[REQUEST_KEY]
        run = await run_store(request).get(run_id_of(params.id))
        if run is not None:
            caller = caller_of(request)
            if not await may(request, caller, run, MANAGE_RUNS):
                raise TaskNotFoundError
            if run["status"] not in ABANDONABLE:
                raise TaskNotCancelableError
            await run_store(request).abandon(
                run["id"], actor=await caller_actor(request, caller)
            )
        return await super().on_cancel_task(params, context)

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        if await self._active_task_registry.get(params.id) is None:
            raise UnsupportedOperationError(
                "Nothing here is following that task now: get it (GetTask) to see where its run is"
            )
        async for event in super().on_subscribe_to_task(params, context):
            yield event


def workflow_a2a_service(*, security: dict[str, Any] | None) -> A2aService:
    """:return: The workflows, for the runtime's A2A router (``ag_…`` names)."""

    async def find(request: Request, name: str, action: CallAction) -> str:
        resolved = await found_workflow(request, name)
        request.state.forge_workflow = resolved
        return str(resolved.record["_id"])

    async def card(request: Request, name: str, url: str) -> AgentCard:
        return workflow_card(request.state.forge_workflow, url, security=security)

    return A2aService(
        prefix=PREFIX,
        find=find,
        card=card,
        executor=WorkflowA2aExecutor(),
        handler=WorkflowRequestHandler,
    )


__all__ = [
    "INPUT_EXTENSION",
    "WorkflowA2aExecutor",
    "WorkflowRequestHandler",
    "run_id_of",
    "update_of",
    "value_of",
    "workflow_a2a_service",
    "workflow_card",
]
