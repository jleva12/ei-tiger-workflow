"""
One run of an ADK workflow, carried on from its ADK session each time the
worker runs its job: its start, every answer to a pause (a decision, a
person's answer, the timer), and every retry.

- **Start.** The run's session (app ``adk_workflows``, the member it acts as,
  the admin API's session ID) is created if it isn't there, and the graph
  runs on the run's input, as a JSON message, until it ends or pauses. The
  run is one ADK invocation of that session, whose ID is the run's (the
  control's ``instance_id``, the run store's run ID): a resubmitted run (the
  same payload, the same session) is another invocation of the session, run
  afresh, and the session's latest invocation is always the latest run's.
- **Pauses.** Its oldest pending pause (``graph.pending_pauses``) is asked of
  the control, then answered in the session, which resumes the same
  invocation (``Runner.run_async(invocation_id=...)``). One gate at a time:
  a run waits at one question at a time.

  - An approval is one of its approval gates (``control.approval``), timed
    out at its deadline. The timeout, or a decision at or after the deadline,
    is the timer's answer (rejected by Forge).
  - Human input is a gate too; the person's answer is the decision's comment,
    as JSON (the admin API held it to the step's response schema). Declined,
    or not JSON, the run fails.
  - A delay whose time has come (or is close enough to sleep in place) is
    woken; before that, the run lets the worker go until then
    (``control.wait_until``).

  Each gate that isn't decided yet ends the attempt by raising the control's
  signal, and the decision starts the job again.
- **Progress.** Each finished step is a note on the run's activity, and a
  checkpoint.
- **End.** The graph's finish (``{outcome, result}``) is the job's result,
  once no way of it still waits: every ending's, when more than one way
  ended. A failed End's fails it, once every way has ended, naming each
  failed End. A :class:`RunFailed` (input that doesn't fit, a step that
  failed with no way to take) fails it at once. A model's or the database's hiccup is a
  :class:`TransientError`: the worker tries again, and the run carries on.
- **Interrupted** (the worker died, a hiccup, or a failed run retried:
  its invocation has events, but no pending pause and no finish): the
  invocation carries on where it stopped; a step that was running, or failed,
  runs again. If ADK can't carry it on, the run starts over in a new session
  (``<session ID>-r<n>``, kept with the job and noted), and its side effects
  may repeat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
import sqlite3
import uuid
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from forge_common.adk.usage import Attribution, UsagePlugin, current_sink, price_from
from google.adk import Event
from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, Session
from google.genai import types
from pydantic import BaseModel, Field

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.graph import (
    AgentBuildError,
    Pause,
    RunFailed,
    RunServices,
    as_data,
    build_agent,
    pending_pauses,
)
from forge_task_adk_workflows.graph.agent_tools import AgentServices, ChildRuns, RunWorkflows
from forge_task_adk_workflows.graph.data import ERROR_KEY
from forge_task_adk_workflows.graph.factories.base import config_of, items, label_of, text
from forge_task_adk_workflows.graph.names import FINISH_NODE, adk_name, body_name
from forge_task_adk_workflows.graph.pauses import REQUEST_INPUT, TOOL_CONFIRMATION
from forge_task_adk_workflows.models import RETRYABLE, UNANSWERED
from forge_tasks.control import ControlSignal, JobControl
from forge_tasks.errors import TaskError, TransientError
from forge_tasks.runner import ok
from forge_tasks.tasks import JobResult

log = logging.getLogger(__name__)

#: The ADK app every run's session is in.
APP_NAME = "adk_workflows"
#: What a run keeps with its job (``control.keep``).
STATE_KEY = "adk_workflow"
#: The most notes a run adds to its activity.
MAX_NOTES = 400
#: How many times a run carries on after an interruption, and starts over
#: when it can't, before it fails.
MAX_RECOVERIES = 10
MAX_RESTARTS = 3
#: The longest ADK session ID (DatabaseSessionService's column).
MAX_SESSION_ID = 128
#: Seconds an approval's timeout waits past its deadline, so it's past it on
#: every worker's clock.
DEADLINE_MARGIN = 1


class RunPayload(BaseModel):
    """One run of an ADK workflow, as the admin API submits it."""

    tenant_id: str = Field(description="The organization the ADK workflow is the organization's")
    agent_id: str
    revision: int = 0
    version: int | str | None = Field(
        default=None, description='Which version of it runs: "draft", or a published version\'s number'
    )
    name: str = ""
    document: dict[str, Any] = Field(description="The ADK workflow (forge.agent/v1) as it was when the run started")
    saved: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="The saved ADK workflows it runs (and calls as tools), by reference (ag_x, ag_x@3, ag_x@draft; "
        "graph.uses.agent_ref), as they were when the run started",
    )
    chat_agents: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="The agents from the Agents page its LLM agents are or call, by reference (ca_x, ca_x@3, "
        "ca_x@draft; graph.uses.agent_ref), each with the saved agents it uses bundled, as they were when the run started",
    )
    knowledge_bases: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="The organization's knowledge bases its LLM agents search, by ID: each one's name and description",
    )
    input: Any = None
    session_id: str = Field(
        min_length=1,
        max_length=MAX_SESSION_ID,
        description="The run's ADK session (a resubmitted task's is the same: it's another invocation of it)",
    )
    run_as: str = Field(min_length=1, description="The member the run acts as: its ADK session's user")
    run_as_name: str = ""
    trigger: dict[str, Any] = Field(default_factory=dict)


class _Refused(Exception):
    """The run can't go on, and trying again won't help: an error that isn't
    a node's :class:`RunFailed` (a model's refusal, ADK's)."""


class AdkRun:
    """
    One attempt of one run: carries it on from its session, until it ends or
    waits.

    :param payload: The run.
    :param control: The run's control (its state, gates, waits and notes).
    :param services: What its nodes use.
    :param sessions: Where its ADK session is.
    :param settings: The task's settings.
    """

    def __init__(
        self,
        payload: RunPayload,
        *,
        control: JobControl,
        services: RunServices,
        sessions: BaseSessionService,
        settings: AdkWorkflowsSettings,
    ) -> None:
        self.payload = payload
        self.control = control
        self.services = services
        self.sessions = sessions
        self.settings = settings
        self.steps = Steps.of(payload.document, payload.saved)
        self.workflow_name = payload.name or text(payload.document.get("name")) or payload.agent_id
        stored = control.load(STATE_KEY) or {}
        self.state: dict[str, Any] = {
            "session_id": stored.get("session_id") or payload.session_id,
            "invocation_id": stored.get("invocation_id") or _invocation_of(control.instance_id),
            "restarts": stored.get("restarts", 0),
            "recoveries": stored.get("recoveries", 0),
            "notes": stored.get("notes", 0),
            "steps": stored.get("steps", 0),
            "finished": stored.get("finished"),
        }
        self.runner: Runner | None = None

    @property
    def session_id(self) -> str:
        return str(self.state["session_id"])

    @property
    def invocation_id(self) -> str:
        return str(self.state["invocation_id"])

    # ------------------------------------------------------------------ running

    async def run(self) -> JobResult:
        """
        Carry the run on: to its end (the job's result), or to its next wait,
        which raises the control's signal.

        :raises TransientError: A hiccup: the queue tries again.
        """
        if self.state["finished"] is not None:
            return self._succeeded(self.state["finished"])
        agents = self._agents()
        if agents is not None:
            self.services = self.services.but(agents=agents)
        try:
            graph = build_agent(self.payload.document, services=self.services, resolve=self.payload.saved.get)
        except AgentBuildError as error:
            return JobResult.failed(f"The ADK workflow can't run: {error}", outcome="failed")
        app = App.model_construct(name=APP_NAME, root_agent=graph, plugins=self._plugins())
        runner = self.runner = Runner(app=app, session_service=self.sessions)
        try:
            await self._save("The run goes on")
            finished = await self._carry_on()
        except RunFailed as failed:
            await self._note(f"The run failed: {failed.message}")
            return JobResult.failed(
                failed.message[:2000],
                outcome="failed",
                result=failed.result,
                step=failed.step,
                session_id=self.session_id,
            )
        except _Refused as refused:
            await self._note(f"The run failed: {refused}")
            return JobResult.failed(f"The run failed: {refused}"[:2000], outcome="failed", session_id=self.session_id)
        finally:
            await runner.close()
            if agents is not None:
                # What its tools opened (MCP sessions): a Workflow's runner doesn't close them.
                await agents.close()
        self.state["finished"] = finished
        outcome = finished.get("outcome") or "succeeded"
        await self._save(f"The run {outcome}")
        await self._note(f"The run failed: {finished['error']}" if finished.get("error") else f"The run {outcome}")
        return self._succeeded(finished)

    def _agents(self) -> AgentServices | None:
        """What the run's LLM agents' tools use: the worker's, with what the run carries."""
        agents: AgentServices | None = self.services.agents
        if agents is None:
            return None
        children: ChildRuns | None = getattr(self.control, "children", None)
        return agents.for_run(
            organization_id=self.payload.tenant_id,
            chat_agents=self.payload.chat_agents,
            knowledge_bases=self.payload.knowledge_bases,
            workflows=RunWorkflows(
                self.payload.model_dump(mode="json"),
                run_id=self.control.instance_id,
                children=children,
                wait=agents.workflow_wait,
            )
            if children is not None
            else None,
        )

    def _plugins(self) -> list[BasePlugin]:
        """What the run's App carries: the usage of its model and tool calls,
        when the worker records it (``forge_common.adk.usage.recording``)."""
        sink = current_sink()
        if sink is None:
            return []
        attribution = Attribution(
            organization_id=self.payload.tenant_id,
            kind="workflow",
            subject_id=self.payload.agent_id,
            subject_name=self.workflow_name,
            run_id=self.control.instance_id or None,
            user_id=self.payload.run_as,
        )
        return [UsagePlugin(sink, attribution, price=price_from(self.services.model))]

    def _succeeded(self, finished: dict[str, Any]) -> JobResult:
        if finished.get("outcome") == "failed":
            return JobResult.failed(
                str(finished.get("error") or "The run ended failed")[:2000],
                outcome="failed",
                result=finished.get("result"),
                step=finished.get("step"),
                session_id=self.session_id,
            )
        return ok(
            outcome="succeeded",
            result=finished.get("result"),
            steps=self.state["steps"],
            session_id=self.session_id,
        )

    async def _carry_on(self) -> dict[str, Any]:
        """The run from where its session is, to its finish (``{outcome, result}``)."""
        await self._begin(await self._open())
        while True:
            session = await self._open()
            # A way that ended doesn't end the run while another waits.
            pauses = pending_pauses(session)
            if not pauses:
                # Without a finish, it ended without reaching an ending that hands one on.
                return _finish_of(session) or {"outcome": "succeeded", "result": None}
            pause = pauses[0]
            response = await self._answer(pause, session)
            await self._drive(pause.answer(response))

    async def _begin(self, session: Session) -> None:
        """The run starts, if it hasn't; if it stopped mid-way, it carries on."""
        if not session.events:
            await self._drive(self._input())
        elif _finish_of(session) is None and not pending_pauses(session):
            await self._recover()

    def _input(self) -> types.Content:
        return types.Content(role="user", parts=[types.Part(text=json.dumps(self.payload.input))])

    async def _recover(self) -> None:
        """The run's invocation, stopped mid-way, carries on where it stopped;
        when ADK can't, the run starts over in a new session."""
        recoveries = int(self.state["recoveries"]) + 1
        if recoveries > MAX_RECOVERIES:
            raise RunFailed(f"The run stopped mid-way {recoveries - 1} times; it doesn't carry on again")
        self.state["recoveries"] = recoveries
        await self._note("The run carries on where it stopped", session_id=self.session_id)
        await self._save("The run carries on where it stopped")
        try:
            await self._drive(None)
        except _Refused as refused:
            await self._start_over(str(refused))

    async def _start_over(self, reason: str) -> None:
        restarts = int(self.state["restarts"]) + 1
        if restarts > MAX_RESTARTS:
            raise RunFailed(f"The run can't carry on where it stopped ({reason}), and it has started over enough")
        new_id = f"{self.payload.session_id}-r{restarts}"
        if len(new_id) > MAX_SESSION_ID:
            new_id = uuid.uuid4().hex
        self.state.update(session_id=new_id, restarts=restarts)
        await self._note(
            f"The run can't carry on where it stopped ({reason}); it starts over in a new session",
            session_id=new_id,
        )
        await self._save("The run starts over")
        await self._begin(await self._open())

    async def _drive(self, message: types.Content | None) -> None:
        """
        The run's invocation, with a message (the input, or an answer), or
        without (carrying it on), until it ends or pauses: each step that
        finishes is a note and a checkpoint.

        :raises RunFailed: The run can't go on.
        :raises TransientError: A hiccup: trying again may help.
        :raises _Refused: Anything else that stopped it.
        """
        assert self.runner is not None
        events: AsyncGenerator[Event] = self.runner.run_async(
            user_id=self.payload.run_as,
            session_id=self.session_id,
            new_message=message,
            invocation_id=self.invocation_id,
        )
        noted: set[str] = set()
        limit = asyncio.timeout(self.settings.run_timeout)
        try:
            async with limit:
                while True:
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        break
                    except (RunFailed, ControlSignal):
                        raise
                    except Exception as error:
                        raise _classified(error) from error
                    await self._progress(event, noted)
        except TimeoutError:
            if limit.expired():
                raise RunFailed(
                    f"The run went on for more than {self.settings.run_timeout:g} seconds without a pause"
                ) from None
            raise
        finally:
            await events.aclose()

    async def _progress(self, event: Event, noted: set[str]) -> None:
        if event.partial or event.error_code or not _hands_on(event):
            return
        for path in _paths(event):
            label = self.steps.label(path)
            if label is None or path in noted:
                continue
            noted.add(path)
            self.state["steps"] = int(self.state["steps"]) + 1
            await self._note(f"{label} finished")
            await self._save(f"{label} finished")

    # ------------------------------------------------------------------ pauses

    async def _answer(self, pause: Pause, session: Session) -> dict[str, Any]:
        """
        The answer to a pause: the timer's, a decision, a person's answer.

        :raises ControlSignal: It isn't answered yet: the run waits.
        :raises RunFailed: Human input declined, or answered with anything but
            a JSON object.
        """
        step = pause.payload.get("step")
        node = self.steps.node(step)
        # A step asks under its ID and its node's path (graph.factories.base.interrupt_id).
        path = pause.interrupt_id.partition("@")[2].partition("#")[0]
        label = self.steps.label(path) or (label_of(node) if node else str(step or pause.interrupt_id))
        step_name = text(node.get("name")) if node else ""
        now = self.services.clock()
        if pause.kind == "delay":
            due = pause.due or now
            if (due - now).total_seconds() > self.services.inline_delay_seconds:
                when = due.isoformat(timespec="seconds")
                await self._note(f"{label} waits until {when}")
                await self.control.wait_until(due, reason=f"{label} waits until {when}")
            return pause.timer_answer()
        details: dict[str, Any] = {
            "step": step,
            "step_name": step_name,
            "workflow_name": self.workflow_name,
            "agent_id": self.payload.agent_id,
            "message": pause.message,
            "session_id": self.session_id,
            "interrupt_id": pause.interrupt_id,
        }
        key = self._gate(pause)
        if pause.kind == TOOL_CONFIRMATION:
            return await self._confirm(pause, key, details)
        if pause.kind == "approval":
            decision = await self.control.approval(
                key=key,
                reason=pause.message or f"Approve {step_name or 'this step'}?",
                details={
                    "kind": "approval",
                    "approvers": pause.payload.get("approvers"),
                    "expires_at": pause.payload.get("expires_at"),
                    **details,
                },
                timeout_seconds=_seconds_until(pause.due, now),
            )
            if pause.due is not None and self.services.clock() >= pause.due:
                await self._note(f"{label}: no decision in time")
                return pause.timer_answer()
            who = decision.actor_name or decision.actor_id or ""
            await self._note(f"{label}: {'approved' if decision.approved else 'rejected'} by {who or 'no one'}")
            return {
                "approved": decision.approved,
                "decided_by": who,
                "comment": decision.comment,
                "decided_at": self.services.clock().isoformat(),
            }
        # Human input, and any other question a node asks.
        decision = await self.control.approval(
            key=key,
            reason=pause.message or f"Answer {step_name or 'this step'}",
            details={
                "kind": "human_input",
                "response_schema": self.steps.response_schema(node) or _asked_schema(session, pause),
                **details,
            },
            timeout_seconds=None,
        )
        who = decision.actor_name or decision.actor_id or ""
        if not decision.approved:
            said = f": {decision.comment}" if decision.comment else ""
            raise RunFailed(f"{label} failed: {who or 'Someone'} declined to answer{said}", step=step)
        try:
            answer = json.loads(decision.comment or "")
        except ValueError:
            raise RunFailed(f"{label} failed: its answer isn't JSON", step=step) from None
        if not isinstance(answer, dict):
            raise RunFailed(f"{label} failed: its answer isn't a JSON object", step=step)
        await self._note(f"{label}: answered by {who or 'someone'}")
        return answer

    async def _confirm(self, pause: Pause, key: str, details: dict[str, Any]) -> dict[str, Any]:
        """
        A tool a person confirms before it's called: an approval of the call
        (the tool, and what the model would call it with), by the
        organization's members.
        """
        found = self.steps.around(pause.path)
        label, node = found if found else (str(pause.payload.get("agent") or "An agent"), None)
        tool = str(pause.payload.get("tool") or "a tool")
        decision = await self.control.approval(
            key=key,
            reason=pause.message,
            details={
                "kind": "approval",
                # Whoever may run the workflow (agents:run) may let its agent call the tool.
                "approvers": "org:member",
                "expires_at": None,
                **details,
                "step": node.get("id") if node else details.get("step"),
                "step_name": text(node.get("name")) if node else details.get("step_name"),
                "tool": tool,
                "args": pause.payload.get("args") or {},
                "confirmation": True,
            },
            timeout_seconds=None,
        )
        who = decision.actor_name or decision.actor_id or ""
        await self._note(
            f"{label}: calling {tool} {'allowed' if decision.approved else 'refused'} by {who or 'no one'}"
        )
        return {"confirmed": decision.approved}

    def _gate(self, pause: Pause) -> str:
        # A run that started over asks its questions afresh.
        restarts = int(self.state["restarts"])
        return f"{pause.interrupt_id}#restart-{restarts}" if restarts else pause.interrupt_id

    # ------------------------------------------------------------------ the session

    async def _open(self) -> Session:
        """
        The run's session, created the first time, with only the run's own
        events: those of its invocation (another task's, of the same payload,
        are other invocations).
        """
        try:
            session = await self.sessions.get_session(
                app_name=APP_NAME, user_id=self.payload.run_as, session_id=self.session_id
            )
            if session is None:
                session = await self.sessions.create_session(
                    app_name=APP_NAME, user_id=self.payload.run_as, session_id=self.session_id
                )
        except (RunFailed, ControlSignal):
            raise
        except Exception as error:
            raise _classified(error) from error
        own = [event for event in session.events if event.invocation_id == self.invocation_id]
        return session.model_copy(update={"events": own})

    async def _save(self, where: str) -> None:
        self.control.keep(STATE_KEY, self.state)
        await self.control.checkpoint(where[:500])

    async def _note(self, message: str, **attributes: Any) -> None:
        if int(self.state["notes"]) >= MAX_NOTES:
            return
        self.state["notes"] = int(self.state["notes"]) + 1
        try:
            await self.control.note(message[:500], **attributes)
        except ControlSignal:
            raise
        except Exception:
            log.warning("couldn't note on the run: %s", message, exc_info=True)


@dataclass(frozen=True)
class Steps:
    """
    A run's steps as its events and pauses name them: the document's by ADK
    name (and its loops'), and every step by ID (the saved workflows' too).
    """

    by_name: Mapping[str, dict[str, Any]]
    loops: frozenset[str]
    by_id: Mapping[str, dict[str, Any]]

    @classmethod
    def of(cls, document: dict[str, Any], saved: Mapping[str, dict[str, Any]]) -> Steps:
        nodes = [node for node in items(document.get("nodes")) if isinstance(node, dict)]
        by_name = {adk_name(text(node.get("name"))): node for node in nodes if node.get("kind") != "start"}
        loops = frozenset(name for name, node in by_name.items() if node.get("kind") == "loop")
        by_id: dict[str, dict[str, Any]] = {}
        for each in [document, *saved.values()]:
            for node in items(each.get("nodes")) if isinstance(each, dict) else []:
                if isinstance(node, dict) and isinstance(node.get("id"), str):
                    by_id.setdefault(node["id"], node)
        return cls(by_name, loops, by_id)

    def node(self, step: Any) -> dict[str, Any] | None:
        return self.by_id.get(step) if isinstance(step, str) else None

    def label(self, path: str) -> str | None:
        """
        :param path: A node's path in the run (``graph@1/each@1/each__each@item_0/shape@1``).
        :return: How the run's activity names the document's step it is
            (``Shape (shape), item 1 of Each``); None for any other node
            (hidden nodes, sub-agents, a saved workflow's steps).
        """
        segments = [segment.partition("@") for segment in path.split("/")[1:]]
        if not segments or len(segments) % 2 == 0:
            return None
        where: list[str] = []
        for index in range(0, len(segments) - 1, 2):
            loop, body = segments[index][0], segments[index + 1]
            if loop not in self.loops or body[0] != body_name(loop):
                return None
            item = body[2].removeprefix("item_")
            name = text(self.by_name[loop].get("name")) or text(self.by_name[loop].get("id"))
            where.append(f"item {int(item) + 1} of {name}" if item.isdigit() else f"in {name}")
        node = self.by_name.get(segments[-1][0])
        if node is None:
            return None
        return ", ".join([label_of(node), *where])

    def around(self, path: str) -> tuple[str, dict[str, Any]] | None:
        """
        :param path: Where in the run something happened: a node's path, or
            deeper (an agent inside an LLM node, a tool's call).
        :return: The document's step it's in, and how the activity names it;
            None outside every step.
        """
        segments = path.split("/")
        for end in range(len(segments), 1, -1):
            label = self.label("/".join(segments[:end]))
            if label is not None:
                node = self.by_name.get(segments[end - 1].partition("@")[0])
                if node is not None:
                    return label, node
        return None

    def response_schema(self, node: dict[str, Any] | None) -> dict[str, Any] | None:
        """:return: What a Human input step's answer is held to, as its document has it."""
        schema = config_of(node).get("response_schema") if node else None
        return schema if isinstance(schema, dict) and schema else None


def _paths(event: Event) -> list[str]:
    # The nodes an event hands on for: its own, or those it's the output of.
    info = event.node_info
    return list(info.output_for or ([info.path] if info.path else []))


def _hands_on(event: Event) -> bool:
    # Whether an event is its node's finishing one: its output, the way it
    # took, its Error way, or an agent's answer; never a question.
    if event.long_running_tool_ids:
        return False
    if event.output is not None:
        return True
    if event.actions is not None and event.actions.route is not None:
        return True
    if ERROR_KEY in (event.custom_metadata or {}):
        return True
    content = event.content
    return bool(
        event.node_info.message_as_output
        and content is not None
        and content.role == "model"
        and not event.get_function_calls()
        and any(part.text and not part.thought for part in content.parts or [])
    )


def _finish_of(session: Session) -> dict[str, Any] | None:
    """:return: The run's finish, ``{outcome, result}``, once a way of its graph
    ended: the finish's last, which has every ending so far."""
    found = None
    for event in session.events:
        if event.output is None:
            continue
        for path in _paths(event):
            segments = path.split("/")
            if len(segments) == 2 and segments[1].partition("@")[0] == FINISH_NODE:
                finished = as_data(event.output)
                found = finished if isinstance(finished, dict) else {"outcome": "succeeded", "result": finished}
    return found


def _asked_schema(session: Session, pause: Pause) -> dict[str, Any] | None:
    # The response schema a node asked with, as ADK has it (a question no step
    # of the document asks: an LLM agent's).
    for event in reversed(session.events):
        for call in event.get_function_calls():
            if call.name == REQUEST_INPUT and call.id == pause.interrupt_id:
                schema = (call.args or {}).get("response_schema")
                return schema if isinstance(schema, dict) else None
    return None


def _invocation_of(task: str | None) -> str:
    """:return: The ADK invocation a run is: the same for every attempt of
    the run, another for another run (a resubmitted one)."""
    if not task:
        return f"e-{uuid.uuid4()}"
    return f"e-{uuid.uuid5(uuid.NAMESPACE_URL, f'forge:adk_workflows:{task}')}"


def _seconds_until(due: datetime | None, now: datetime) -> int | None:
    if due is None:
        return None
    return max(0, math.ceil((due - now).total_seconds())) + DEADLINE_MARGIN


def _classified(error: Exception) -> Exception:
    """What an error that stopped a run means for it: try again later
    (:class:`TransientError`), or the run can't go on (:class:`_Refused`)."""
    message = str(getattr(error, "message", "") or error)[:500] or type(error).__name__
    if _transient(error):
        return TransientError(f"The run can't go on for now ({type(error).__name__}): {message}")
    log.warning("adk_workflows: a run stopped: %s", message, exc_info=error)
    return _Refused(f"{type(error).__name__}: {message}")


def _transient(error: BaseException) -> bool:
    if isinstance(error, TaskError):
        return not error.permanent
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and (status in RETRYABLE or status >= 500):
        return True
    if isinstance(error, httpx.TransportError | ConnectionError | TimeoutError | socket.gaierror):
        return True
    if isinstance(error, sqlite3.OperationalError) or type(error).__name__ in UNANSWERED:
        return True
    try:
        from sqlalchemy import exc as db
    except ImportError:  # pragma: no cover - a dependency
        return False
    if isinstance(error, db.DBAPIError) and error.connection_invalidated:
        return True
    return isinstance(error, db.OperationalError | db.InterfaceError | db.DisconnectionError | db.TimeoutError)
