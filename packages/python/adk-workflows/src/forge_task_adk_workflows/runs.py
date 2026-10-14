"""
One run of an ADK workflow, carried on from its ADK session each time the
worker runs its job: its start, every answer to a pause (a decision, a
person's answer, the timer), and every retry.

- **Start.** The run's session (app ``adk_workflows``, the member it acts as,
  the admin API's session ID) is created if it isn't there, and the graph
  runs on the run's input, as a JSON message, until it ends or pauses. The
  run is one ADK invocation of that session, whose ID is the run's (the
  control's ``instance_id``, the run store's run ID). A resubmitted run has a
  session of its own; one resubmitted before they did shares its original's,
  as another invocation of it, run afresh.
- **Pauses.** Its oldest pending pause (``graph.pending_pauses``) is asked of
  the control, then answered in the session, which resumes the same
  invocation (``Runner.run_async(invocation_id=...)``). One gate at a time:
  a run waits at one question at a time.

  - An approval is one of its approval gates (``control.approval``), timed
    out at its deadline. The timeout, or a decision made at or after the
    deadline (when it was made, not when the run reads it), is the timer's
    answer (rejected by Forge).
  - Human input is a gate too; the person's answer is the decision's comment,
    as JSON (the admin API held it to the step's response schema). Declined,
    or not JSON, the run fails.
  - A delay whose time has come (or is close enough to sleep in place) is
    woken; before that, the run lets the worker go until then
    (``control.wait_until``).

  Each gate that isn't decided yet ends the attempt by raising the control's
  signal, and the decision starts the job again.

  ADK hands an answer to the newest invocation of the session that asked
  under its ID, so a run whose question another run of its session asked
  since fails instead of answering it (the answer would carry the other run
  on); and an answered question still waiting fails the run instead of being
  answered again.
- **Progress.** Each finished step is a note on the run's activity, and a
  checkpoint. A run notes MAX_NOTES lines at most, then one that says what
  it does next isn't noted; a note that can't be written is counted
  (``notes_lost``). Either way, its activity isn't all there.
- **End.** The graph's finish (``{outcome, result}``) is the job's result,
  once its invocation has ended, every way of it: every ending's, when more
  than one way ended. A finish recorded while another way was still going
  isn't the run's end. A failed End's fails it, once every way has ended,
  naming each failed End. A :class:`RunFailed` (input that doesn't fit, a
  step that failed with no way to take) fails it at once. A model's or the
  database's hiccup is a :class:`TransientError`: the worker tries again, and
  the run carries on.
- **Interrupted** (the worker died, a hiccup, or a failed run retried: its
  invocation has events, but no pending pause): the invocation carries on
  where it stopped; a step that was running, or failed, runs again, and the
  ways that ended stay ended. An invocation that had ended carries on to its
  end again, running nothing. If ADK can't carry it on (it refuses before
  anything runs), the run fails, to be resubmitted; or, when its workflow
  says so (its start's ``start_over``), it starts over in a new session
  (``<session ID>-r<n>``, kept with the job and noted), and its steps run
  again. A step that fails once the invocation carried on fails the run.
- **Tool calls.** Each call an LLM agent's tool takes is noted with the job,
  durably, before it runs (unless the tool only reads: ``reads_only``), until
  its answer is in the session. ADK runs a step its worker stopped mid-call
  again, asking its model afresh, so the model would call again, blind, under
  a new ID. Each attempt answers first the calls an earlier one left
  unanswered, so the model sees what became of them: a workflow's call is
  answered with the run it started (``child_run_id``: a run's call starts one
  run, whatever becomes of the call), waited for as the tool waits; any
  other, as cut off (noted: it may have taken effect) or unanswered (it
  wasn't, or only read). Questions, and calls a person is still to confirm,
  wait. An agent that sees only its own turn (``include_contents`` none, as
  agents from the Agents page) doesn't see any answer an earlier attempt's
  call got, cut off or not, when its step runs again (after a model's
  hiccup, a stop, a failed retry): each such call that took effect, or may
  have, is journaled with the job (``replays``), durably, until its step
  ends, and the same call again (the same tool, arguments and agent, at the
  same step) is answered as it was, once each attempt, instead of running.
- **Files.** The files a run starts with are artifacts of the session the
  admin API saved them in (``files``): its own, or its original's for a
  resubmitted run. The runner keeps artifacts where the run's session is,
  so its nodes read them there: a run in another session (resubmitted, or
  started over) has them copied to it before it goes on, and a copy that
  failed is finished by its next attempt.
- **File system.** A run whose start (or a saved workflow's it runs) allows
  a file system has its folder made before it goes on (``workspaces``),
  named by its run ID, so every attempt works in the same one.
  One with a Bash step fails before it starts on a worker that doesn't run
  commands (``commands`` off).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import socket
import sqlite3
import uuid
from collections.abc import AsyncGenerator, Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from forge_common.adk.context_window import ContextWindowPlugin, windows_from
from forge_common.adk.usage import Attribution, UsagePlugin, current_sink, price_from
from google.adk import Event
from google.adk.apps import App
from google.adk.artifacts import BaseArtifactService
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, Session
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from pydantic import BaseModel, Field

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.files import RunFile, copy_run_files
from forge_task_adk_workflows.graph import (
    AgentBuildError,
    Pause,
    RunFailed,
    RunServices,
    as_data,
    build_agent,
    pending_pauses,
)
from forge_task_adk_workflows.graph.agent_tools import (
    CLOSE_SECONDS,
    AgentServices,
    ChildRuns,
    RunWorkflows,
    child_run_id,
    reads_only,
    starts_runs,
)
from forge_task_adk_workflows.graph.data import ERROR_KEY
from forge_task_adk_workflows.graph.factories.base import config_of, items, label_of, text
from forge_task_adk_workflows.graph.names import BODY_KINDS, FINISH_NODE, adk_name, body_name
from forge_task_adk_workflows.graph.pauses import REQUEST_CONFIRMATION, REQUEST_INPUT, TOOL_CONFIRMATION
from forge_task_adk_workflows.graph.uses import agent_ref
from forge_task_adk_workflows.models import RETRYABLE, UNANSWERED
from forge_task_adk_workflows.support.commands import NO_COMMANDS
from forge_task_adk_workflows.workspaces import WorkspaceError, make_workspace, runs_commands, uses_file_system
from forge_tasks.control import ControlSignal, JobControl
from forge_tasks.errors import TaskError, TransientError
from forge_tasks.runner import ok
from forge_tasks.tasks import JobResult

log = logging.getLogger(__name__)

#: The ADK app every run's session is in.
APP_NAME = "adk_workflows"
#: What a run keeps with its job (``control.keep``).
STATE_KEY = "adk_workflow"
#: The most notes a run adds to its activity, before the one that says so.
MAX_NOTES = 400
#: How many times a run carries on after an interruption, and starts over
#: when it can't (its workflow starting over), before it fails.
MAX_RECOVERIES = 10
MAX_RESTARTS = 3
#: The most of an unexpected error's message a run's failure keeps.
MAX_ERROR = 1500
#: The longest ADK session ID (DatabaseSessionService's column).
MAX_SESSION_ID = 128
#: What a call its worker's stop cut off is answered, as a guarded tool's
#: failure (``forge_common.adk.toolset``): noted before it ran, it may have
#: taken effect; else it didn't, or only read.
CUT_OFF = "This call was cut off: the run's worker stopped while it ran, so it may have taken effect, or not."
CHECK_FIRST = ("Check whether it took effect before calling it again.",)
NO_ANSWER = "This call got no answer: the run's worker stopped."
CALL_AGAIN = ("Call it again if it's still needed.",)
#: How an answer the run gave a call cut off is marked in the session (``custom_metadata``).
CUT_OFF_KEY = "forge_cut_off"
#: Seconds an approval's timeout waits past its deadline, so it's past it on
#: every worker's clock.
DEADLINE_MARGIN = 1
NO_ARTIFACTS = (
    "set HYBRID_ADK_WORKFLOWS__ARTIFACTS to where the admin API keeps them "
    "(its FORGE_ADMIN_WORKFLOW_ARTIFACTS), with the S3 service's settings"
)
NO_WORKSPACES = "set HYBRID_ADK_WORKFLOWS__WORKSPACES to a folder on a volume every worker reaches"


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
    skills: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="The organization's skills its LLM agents' Skills tools use, by ID: each whole "
        "(forge_agent_runtime.SkillBundle), as it was when the run started",
    )
    input: Any = None
    files: list[RunFile] = Field(
        default_factory=list,
        description="The files it starts with: artifacts the admin API saved in files_session_id "
        "(or, without one, session_id)",
    )
    session_id: str = Field(
        min_length=1,
        max_length=MAX_SESSION_ID,
        description="The run's ADK session: its own, a resubmitted run's too (one resubmitted before they were "
        "shares its original's: it's another invocation of it)",
    )
    files_session_id: str | None = Field(
        default=None,
        max_length=MAX_SESSION_ID,
        description="The session its files were saved in, when it isn't session_id: a resubmitted run's "
        "original's. The run copies them to its own before it goes on",
    )
    run_as: str = Field(min_length=1, description="The member the run acts as: its ADK session's user")
    run_as_name: str = ""
    trigger: dict[str, Any] = Field(default_factory=dict)


class _Refused(Exception):
    """The run can't go on, and trying again won't help: an error that isn't
    a node's :class:`RunFailed` (a model's refusal, ADK's).

    :ivar began: Whether its invocation got going (ADK handed on any of its
        events) first: a step failed, rather than ADK refusing to carry the
        invocation on. A step that raises hands on its error event first.
    :ivar step: The run's step it happened in, when it was one.
    """

    began = False
    step: str | None = None


class AdkRun:
    """
    One attempt of one run: carries it on from its session, until it ends or
    waits.

    :param payload: The run.
    :param control: The run's control (its state, gates, waits and notes).
    :param services: What its nodes use.
    :param sessions: Where its ADK session is.
    :param settings: The task's settings.
    :param artifacts: Where its ADK artifacts are (the files it starts with
        among them); None when the worker keeps none, and a run with files
        fails.
    """

    def __init__(
        self,
        payload: RunPayload,
        *,
        control: JobControl,
        services: RunServices,
        sessions: BaseSessionService,
        settings: AdkWorkflowsSettings,
        artifacts: BaseArtifactService | None = None,
        artifacts_unavailable: str | None = None,
    ) -> None:
        self.payload = payload
        self.control = control
        self.services = services
        self.sessions = sessions
        self.settings = settings
        self.artifacts = artifacts
        self.artifacts_unavailable = artifacts_unavailable
        self.steps = Steps.of(payload.document, payload.saved)
        self.workflow_name = payload.name or text(payload.document.get("name")) or payload.agent_id
        self.starts_over = _starts_over(payload.document)
        stored = control.load(STATE_KEY) or {}
        self.state: dict[str, Any] = {
            "session_id": stored.get("session_id") or payload.session_id,
            "invocation_id": stored.get("invocation_id") or _invocation_of(control.instance_id),
            "restarts": stored.get("restarts", 0),
            "recoveries": stored.get("recoveries", 0),
            "notes": stored.get("notes", 0),
            # Notes that couldn't be written: the run's activity misses them.
            "notes_lost": stored.get("notes_lost", 0),
            "steps": stored.get("steps", 0),
            "finished": stored.get("finished"),
            # The session the run's files were last copied to (``_take_files``).
            "files_session": stored.get("files_session"),
            # The sessions it started over from, oldest first (the run page shows them).
            "earlier_sessions": stored.get("earlier_sessions", []),
            # The tool calls noted before they ran, by ID, until their answer is in the session.
            "calls": dict(stored.get("calls") or {}),
            # The calls of agents that see only their own turn that took effect, or may have, in
            # steps still going ({key, call, child}): answered again, should the agent call again.
            "replays": list(stored.get("replays") or []),
        }
        self.runner: Runner | None = None
        #: The invocation that asked each question last, across the session (``_open``).
        self.askers: dict[str, str] = {}
        #: This attempt's answers to the journaled calls (``replays``), by call
        #: (:func:`_call_key`), each used once, for an agent that doesn't see them asking again.
        self.replays: dict[tuple[str, str, str, str], list[_Replay]] = {}

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
        if self.payload.files and self.artifacts is None:
            return JobResult.failed(
                "The run started with files, and this worker can't read them: "
                + (self.artifacts_unavailable or NO_ARTIFACTS),
                outcome="failed",
            )
        if self.payload.files:
            self.services = self.services.but(files=tuple(self.payload.files))
        if runs_commands(self.payload.document, self.payload.saved) and not self.services.commands:
            return JobResult.failed(
                "The workflow has Bash steps, and this worker doesn't run commands: " + NO_COMMANDS,
                outcome="failed",
            )
        if uses_file_system(self.payload.document, self.payload.saved):
            if self.settings.workspaces is None:
                return JobResult.failed(
                    "The workflow works in a file system, and this worker has none: " + NO_WORKSPACES,
                    outcome="failed",
                )
            try:
                # The run's own: the same folder for each of its attempts.
                folder = make_workspace(
                    self.settings.workspaces,
                    organization=self.payload.tenant_id,
                    workflow=self.payload.agent_id,
                    run=self.control.instance_id or self.payload.session_id,
                )
            except WorkspaceError as error:
                return JobResult.failed(f"The run's file system can't be used: {error}", outcome="failed")
            self.services = self.services.but(workspace=str(folder))
        agents = self._agents()
        if agents is not None:
            self.services = self.services.but(agents=agents)
        self.services = self.services.but(
            tool_calls=self._note_call,
            organization_id=self.payload.tenant_id,
            # Whom its model and tool calls are for: Pi agent steps record theirs too.
            attribution=self._attribution(),
            # Its LLM agents read and save files wherever this worker keeps artifacts.
            artifacts=self.artifacts is not None,
        )
        try:
            graph = build_agent(self.payload.document, services=self.services, resolve=self.payload.saved.get)
        except AgentBuildError as error:
            return JobResult.failed(f"The ADK workflow can't run: {error}", outcome="failed")
        app = App.model_construct(name=APP_NAME, root_agent=graph, plugins=self._plugins())
        runner = self.runner = Runner(app=app, session_service=self.sessions, artifact_service=self.artifacts)
        try:
            await self._save("The run goes on")
            await self._take_files()
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
            return JobResult.failed(
                f"The run failed: {refused}"[:2000], outcome="failed", step=refused.step, session_id=self.session_id
            )
        finally:
            await _closed("its runner", runner.close())
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
            skills=self.payload.skills,
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
        """What the run's App carries: each LLM agent's context window, kept in
        the state and compacted mid-step (``forge_common.adk.context_window``),
        and the usage of its model and tool calls, when the worker records it
        (``forge_common.adk.usage.recording``)."""
        threshold = self.services.compaction_threshold
        plugins: list[BasePlugin] = [
            ContextWindowPlugin(
                window=windows_from(self.services.model),
                threshold=threshold or 1.0,
                auto=threshold is not None,
            )
        ]
        sink = current_sink()
        if sink is None:
            return plugins
        return [*plugins, UsagePlugin(sink, self._attribution(), price=price_from(self.services.model))]

    def _attribution(self) -> Attribution:
        """:return: Whom the run's model and tool calls are for: its organization, its workflow and its member."""
        return Attribution(
            organization_id=self.payload.tenant_id,
            kind="workflow",
            subject_id=self.payload.agent_id,
            subject_name=self.workflow_name,
            run_id=self.control.instance_id or None,
            user_id=self.payload.run_as,
        )

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
        session = await self._open()
        if await self._answer_cut_off(session):
            session = await self._open()
        self._replays_from(session)
        await self._begin(session)
        answered: set[str] = set()
        while True:
            session = await self._open()
            # A way that ended doesn't end the run while another waits.
            pauses = pending_pauses(session)
            if not pauses:
                # Its invocation ended, every way of it (_begin carried on one that hadn't).
                # Without a finish, it ended without reaching an ending that hands one on.
                return _finish_of(session) or {"outcome": "succeeded", "result": None}
            pause = pauses[0]
            step = pause.payload.get("step")
            if self.askers.get(pause.interrupt_id, self.invocation_id) != self.invocation_id:
                raise RunFailed(
                    f"{self._label(pause)}: another run of this session waits at the same step, so the answer "
                    "would go to that run. Let that run finish or abandon it, then resubmit this one",
                    step=step,
                )
            if pause.interrupt_id in answered:
                raise RunFailed(
                    f"{self._label(pause)}: its answer didn't reach the run, so the run stopped instead of "
                    "answering again",
                    step=step,
                )
            response = await self._answer(pause, session)
            answered.add(pause.interrupt_id)
            await self._drive(pause.answer(response))

    async def _begin(self, session: Session) -> None:
        """The run starts, if it hasn't; if it stopped mid-way (its invocation
        has events, and asks nothing), it carries on, even past a finish: one
        way's ending doesn't mean the others ended. An invocation that had
        ended carries on to its end again, running nothing."""
        if not session.events:
            await self._drive(self._input())
        elif not pending_pauses(session):
            await self._recover()

    def _input(self) -> types.Content:
        return types.Content(role="user", parts=[types.Part(text=json.dumps(self.payload.input))])

    async def _recover(self) -> None:
        """
        The run's invocation, stopped mid-way, carries on where it stopped. A
        step that fails once it carried on fails the run. When ADK can't carry
        it on (it refuses before anything runs), the run fails, to be
        resubmitted; or starts over in a new session, when its workflow says
        so (its start's ``start_over``): its steps run again.
        """
        recoveries = int(self.state["recoveries"]) + 1
        if recoveries > MAX_RECOVERIES:
            raise RunFailed(f"The run stopped mid-way {recoveries - 1} times; it doesn't carry on again")
        self.state["recoveries"] = recoveries
        await self._note("The run carries on where it stopped", session_id=self.session_id)
        await self._save("The run carries on where it stopped")
        try:
            await self._drive(None)
        except _Refused as refused:
            if refused.began:
                raise
            if not self.starts_over:
                raise RunFailed(
                    f"The run can't carry on where it stopped ({refused}). Resubmit it to run it again from the start"
                ) from None
            await self._start_over(str(refused))

    async def _start_over(self, reason: str) -> None:
        restarts = int(self.state["restarts"]) + 1
        if restarts > MAX_RESTARTS:
            raise RunFailed(f"The run can't carry on where it stopped ({reason}), and it has started over enough")
        new_id = f"{self.payload.session_id}-r{restarts}"
        if len(new_id) > MAX_SESSION_ID:
            new_id = uuid.uuid4().hex
        earlier = [*self.state["earlier_sessions"], self.session_id]
        # Its steps run again from the start, their calls with them: the journal was the old session's.
        self.state.update(session_id=new_id, restarts=restarts, earlier_sessions=earlier, replays=[])
        self.replays = {}
        await self._note(
            f"The run can't carry on where it stopped ({reason}); it starts over in a new session",
            session_id=new_id,
        )
        await self._save("The run starts over")
        await self._take_files()
        await self._begin(await self._open())

    async def _take_files(self) -> None:
        """
        The run's files, copied to the session it runs in from the one the
        admin API saved them in, unless they're there already: its own, or an
        earlier attempt copied them. A copy that fails is tried again by the
        run's next attempt, before it goes on.
        """
        if not self.payload.files:
            return
        source = self.payload.files_session_id or self.payload.session_id
        if self.session_id in (source, self.state["files_session"]):
            return
        assert self.artifacts is not None
        try:
            await copy_run_files(
                self.artifacts,
                app_name=APP_NAME,
                user_id=self.payload.run_as,
                from_session=source,
                to_session=self.session_id,
                files=self.payload.files,
            )
        except FileNotFoundError as error:
            raise RunFailed(f"The run can't go on without its files: {error}") from None
        except Exception as error:
            raise _classified(error) from error
        self.state["files_session"] = self.session_id
        await self._save("Its files are in its session")

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
        began = False
        # Where the last error happened: ADK records a step's error before the run raises it.
        failed_at: str | None = None
        limit = asyncio.timeout(self.settings.run_timeout)
        try:
            async with limit:
                while True:
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        break
                    except ControlSignal:
                        raise
                    except RunFailed as failure:
                        raise self._placed(failure, failed_at) from None
                    except Exception as error:
                        failure = _classified(error)
                        if isinstance(failure, _Refused):
                            failure.began = began
                            failure = self._placed_refused(failure, failed_at)
                        raise failure from error
                    began = True
                    if event.error_code or event.error_message:
                        failed_at = event.node_info.path or failed_at
                    await self._progress(event, noted)
        except TimeoutError:
            if limit.expired():
                raise RunFailed(
                    f"The run went on for more than {self.settings.run_timeout:g} seconds without a pause"
                ) from None
            raise
        finally:
            await _closed("its events", events.aclose())

    def _placed(self, failure: RunFailed, path: str | None) -> RunFailed:
        """
        A step's failure as the run's: at the run's own step it happened in.
        A saved workflow's step that failed is its node's failure (its step's
        ID is the workflow's, not the run's), saying where in it.

        :param path: Where its error event says it happened.
        """
        found = self.steps.around(path) if path else None
        if found is None:
            return failure
        label, node = found
        step = text(node.get("id")) or None
        own = path is not None and self.steps.label(path) is not None
        if node.get("kind") == "saved" and not own:
            workflow = self._saved_name(node)
            return RunFailed(
                f"{label} failed in its workflow {workflow}: {failure.message}", result=failure.result, step=step
            )
        if failure.step == step or (own and failure.step is not None):
            return failure
        return RunFailed(failure.message, result=failure.result, step=step)

    def _placed_refused(self, refused: _Refused, path: str | None) -> _Refused:
        # An error a step raised (a model's, a library's), named by the run's step it happened in.
        found = self.steps.around(path) if path else None
        if found is None:
            return refused
        label, node = found
        where = (
            f"{label} failed in its workflow {self._saved_name(node)}"
            if node.get("kind") == "saved" and path is not None and self.steps.label(path) is None
            else f"{label} failed"
        )
        placed = _Refused(f"{where}: {refused}")
        placed.began = refused.began
        placed.step = text(node.get("id")) or None
        return placed

    def _saved_name(self, node: dict[str, Any]) -> str:
        # A saved workflow's node's workflow, by name: "Look up (ag_x@3)".
        config = config_of(node)
        ref = agent_ref(text(config.get("agent")), config.get("version"))
        name = text((self.payload.saved.get(ref) or {}).get("name"))
        return f"{name} ({ref})" if name else ref

    async def _progress(self, event: Event, noted: set[str]) -> None:
        for response in event.get_function_responses():
            # Answered: its answer is in the session (kept at the next checkpoint), and journaled.
            called = self.state["calls"].pop(response.id or "", None)
            if called is not None:
                self._journal(str(response.id), called)
        if event.partial or event.error_code or not _hands_on(event):
            return
        for path in _paths(event):
            label = self.steps.label(path)
            if label is None or path in noted:
                continue
            noted.add(path)
            # Ended: it doesn't run again, nor do its calls.
            self.state["replays"] = [
                entry for entry in self.state["replays"] if not _within(str(entry["key"][0]), path)
            ]
            self.state["steps"] = int(self.state["steps"]) + 1
            await self._note(f"{label} finished")
            await self._save(f"{label} finished")

    # ------------------------------------------------------------------ tool calls

    async def _note_call(self, tool: Any, args: dict[str, Any], tool_context: Any) -> dict[str, Any] | None:
        """
        An LLM agent's tool is about to run (ADK's ``before_tool_callback``,
        before a person confirms it too): unless it only reads, the call is
        noted, durably, until its answer is in the session. The worker
        stopping while it runs leaves it unanswered, and the next attempt
        answers it as cut off (:meth:`_answer_cut_off`).

        :return: None: the tool runs. Or the answer to the call an earlier
            attempt left unanswered, made again by an agent that doesn't see
            it (:meth:`_replayed`), unless it only reads: the tool doesn't run.
        """
        if reads_only(tool):
            return None  # it runs, again if need be: nothing to note
        agent = getattr(tool_context.get_invocation_context(), "agent", None)
        key = _call_key(str(tool_context.node_path or ""), str(getattr(agent, "name", "")), tool.name, args)
        own_turn = getattr(agent, "include_contents", "default") == "none"
        replayed = await self._replayed(key, tool, tool_context) if own_turn else None
        if replayed is not None:
            return replayed
        call_id = getattr(tool_context, "function_call_id", None)
        if not call_id or call_id in self.state["calls"]:
            return None
        noted: dict[str, Any] = {"tool": tool.name, "at": self.services.clock().isoformat(), "key": list(key)}
        if starts_runs(tool):
            noted["starts_run"] = True
        if own_turn:
            noted["own_turn"] = True
        self.state["calls"][call_id] = noted
        await self._save(f"It calls {tool.name}")
        return None

    async def _replayed(self, key: tuple[str, str, str, str], tool: Any, tool_context: Any) -> dict[str, Any] | None:
        """
        :return: The answer a journaled call got, when the agent that made it,
            which doesn't see it (its own turn only), makes it again: the same
            tool, arguments and agent at the same step, once each attempt;
            None for any other call, which runs. A workflow's is its run's, as
            it is now.
        """
        waiting = self.replays.get(key)
        if not waiting:
            return None
        replay = waiting.pop(0)
        found = self.steps.around(key[0])
        how = "as the one its worker's stop cut off" if replay.cut_off else "as it was before: it isn't made twice"
        await self._note(
            f"{found[0] if found else 'A step'}: its call to {tool.name} again, as it carried on, is answered {how}",
            call=getattr(tool_context, "function_call_id", None),
        )
        if replay.child is not None:
            workflows: RunWorkflows | None = getattr(getattr(self.services, "agents", None), "workflows", None)
            if workflows is not None:
                return {"status": "success", "payload": await workflows.attach(replay.child)}
        return replay.answer

    async def _answer_cut_off(self, session: Session) -> bool:
        """
        The tool calls an earlier attempt noted, settled before the run goes
        on. Those it left unanswered (its worker stopped while they ran) are
        answered in the session, where the call is: a workflow's with the run
        it started, any other as cut off or unanswered (:meth:`_cut_off`).
        Questions, a tool's long-running calls and the calls a person is
        still to confirm wait for their answer. Each that took effect, or may
        have (answered, cut off, or a workflow's that started its run), is
        journaled for an agent that sees only its own turn (:meth:`_journal`).

        :param session: The run's session, with its own events (:meth:`_open`).
        :return: Whether it answered any.
        """
        own = session.events
        answered = {response.id for event in own for response in event.get_function_responses() if response.id}
        waiting = _confirming(own, answered)
        noted: dict[str, dict[str, Any]] = self.state["calls"]
        changed, any_answered = bool(noted), False
        for event in list(own):
            long_running = set(event.long_running_tool_ids or ())
            for call in event.get_function_calls():
                if not call.id or call.name in (REQUEST_INPUT, REQUEST_CONFIRMATION):
                    continue
                path = str(getattr(event.node_info, "path", "") or "")
                key = _call_key(path, event.author, call.name or "", call.args or {})
                if call.id in answered:
                    if call.id in noted:
                        # Its answer reached the session, the attempt's end didn't reach the journal.
                        self._journal(call.id, noted[call.id], key=key)
                    continue
                if call.id in long_running or call.id in waiting:
                    continue
                answer, said, child = await self._cut_off(call)
                try:
                    await self.sessions.append_event(session, _answering(event, call, answer))
                except Exception as error:
                    raise _classified(error) from error
                answered.add(call.id)
                changed = any_answered = True
                if call.id in noted or child is not None:
                    self._journal(call.id, noted.get(call.id, {}), key=key, starts_run=child is not None)
                found = self.steps.around(path)
                await self._note(f"{found[0] if found else 'A step'}: {said}", call=call.id)
        # Every call noted before this attempt is answered now, one way or the other.
        self.state["calls"] = {}
        if changed:
            await self._save("Its calls an earlier attempt left unanswered are answered")
        return any_answered

    def _journal(
        self,
        call_id: str,
        noted: dict[str, Any],
        *,
        key: tuple[str, str, str, str] | None = None,
        starts_run: bool = False,
    ) -> None:
        """
        A call that took effect, or may have, journaled (``replays``, kept at
        the next checkpoint) until its step ends, when its agent sees only its
        own turn: should the agent make it again, it's answered as it was
        (:meth:`_replays_from`). A call noted before notes said whose agent it
        was is journaled whatever its agent.

        :param noted: What :meth:`_note_call` noted of it (nothing, for a
            workflow's call that started its run unnoted).
        :param key: The call as the session has it, when the note doesn't.
        """
        if not noted.get("own_turn", "key" not in noted):
            return
        found = noted.get("key") or key
        if not found or any(entry["call"] == call_id for entry in self.state["replays"]):
            return
        entry: dict[str, Any] = {"key": list(found), "call": call_id}
        if starts_run or noted.get("starts_run"):
            entry["starts_run"] = True
        self.state["replays"].append(entry)

    def _replays_from(self, session: Session) -> None:
        """
        This attempt's answers to the journaled calls, each used once: as the
        session has it (the tool's own answer, or the one the run gave it cut
        off); a workflow's is its run's as it is when it's used. One the
        session doesn't have (it can't be) is answered as cut off.

        :param session: The run's session, with its own events, cut-off calls answered.
        """
        answers = {
            response.id: (event, response)
            for event in session.events
            for response in event.get_function_responses()
            if response.id
        }
        self.replays = {}
        for entry in self.state["replays"]:
            found = answers.get(entry["call"])
            if found is None:
                answer, cut_off = {"status": "failed", "reason": CUT_OFF, "suggested_fixes": list(CHECK_FIRST)}, True
            else:
                event, response = found
                answer, cut_off = dict(response.response or {}), bool((event.custom_metadata or {}).get(CUT_OFF_KEY))
            child = _run_of(answer) if entry.get("starts_run") else None
            self.replays.setdefault(_key_of(entry["key"]), []).append(_Replay(answer, child, cut_off))

    async def _cut_off(self, call: types.FunctionCall) -> tuple[dict[str, Any], str, str | None]:
        """
        :return: What a call an earlier attempt left unanswered is answered,
            as its guarded tool answers; what the run notes of it; and the
            run it started, a workflow's.
        """
        name, call_id = call.name or "a tool", str(call.id)
        noted = self.state["calls"].get(call_id)
        workflows: RunWorkflows | None = getattr(getattr(self.services, "agents", None), "workflows", None)
        if workflows is not None:
            child = child_run_id(self.control.instance_id, call_id)
            if await workflows.children.get(child) is not None:
                # The run that call started: never another for it.
                answer = await workflows.attach(child)
                said = f"its call to {name} was cut off; it's answered with the run it started ({child[:8]})"
                return {"status": "success", "payload": answer}, said, child
        if noted is not None and not noted.get("starts_run"):
            # Noted before it ran, it may have taken effect. (A workflow's call
            # whose run isn't there didn't: what it does is start that run.)
            said = f"its call to {name} was cut off as its worker stopped: it may have taken effect"
            return {"status": "failed", "reason": CUT_OFF, "suggested_fixes": list(CHECK_FIRST)}, said, None
        said = f"its call to {name} got no answer: its worker stopped"
        return {"status": "failed", "reason": NO_ANSWER, "suggested_fixes": list(CALL_AGAIN)}, said, None

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
        label = self._label(pause)
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
            # Held to when it was made, not when the run reads it: a decision in
            # time whose job waited past the deadline stands.
            decided_at = _utc(decision.decided_at or self.services.clock())
            if pause.due is not None and decided_at >= _utc(pause.due):
                await self._note(f"{label}: no decision in time")
                return pause.timer_answer()
            who = decision.actor_name or decision.actor_id or ""
            await self._note(f"{label}: {'approved' if decision.approved else 'rejected'} by {who or 'no one'}")
            return {
                "approved": decision.approved,
                "decided_by": who,
                "comment": decision.comment,
                "decided_at": decided_at.isoformat(),
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

    def _label(self, pause: Pause) -> str:
        """How the run's activity names the step a pause is at."""
        step = pause.payload.get("step")
        node = self.steps.node(step)
        # A step asks under its ID and its node's path (graph.factories.base.interrupt_id).
        path = pause.interrupt_id.partition("@")[2].partition("#")[0]
        return self.steps.label(path) or (label_of(node) if node else str(step or pause.interrupt_id))

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
        self.askers = {
            call.id: event.invocation_id for event in session.events for call in event.get_function_calls() if call.id
        }
        return session.model_copy(update={"events": own})

    async def _save(self, where: str) -> None:
        self.control.keep(STATE_KEY, self.state)
        await self.control.checkpoint(where[:500])

    async def _note(self, message: str, **attributes: Any) -> None:
        """A line on the run's activity: MAX_NOTES of them, then one that says
        the rest aren't noted. One that can't be written is counted."""
        noted = int(self.state["notes"])
        if noted > MAX_NOTES:
            return
        self.state["notes"] = noted + 1
        if noted == MAX_NOTES:
            message, attributes = f"{MAX_NOTES} notes so far: what the run does next isn't noted", {}
        try:
            await self.control.note(message[:500], **attributes)
        except ControlSignal:
            raise
        except Exception:
            self.state["notes_lost"] = int(self.state["notes_lost"]) + 1
            # Kept at once: the run's next write has it, its last one too.
            self.control.keep(STATE_KEY, self.state)
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
        loops = frozenset(name for name, node in by_name.items() if node.get("kind") in BODY_KINDS)
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


@dataclass(frozen=True)
class _Replay:
    """The answer a journaled call got, the run it started (a workflow's), and
    whether the run answered it, cut off."""

    answer: dict[str, Any]
    child: str | None
    cut_off: bool = True


def _call_key(path: str, agent: str, tool: str, args: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """:return: A call, as the same call again is known: where (the node's
    path), by which agent, to which tool, with what arguments (their digest,
    so the journal stays small)."""
    made = json.dumps(as_data(dict(args)), sort_keys=True, default=str)
    return path, agent, tool, hashlib.sha256(made.encode()).hexdigest()


def _key_of(kept: Any) -> tuple[str, str, str, str]:
    path, agent, tool, args = (str(part) for part in kept)
    return path, agent, tool, args


def _within(path: str, ended: str) -> bool:
    """:return: Whether ``path`` is the node at ``ended``, or inside it."""
    return path == ended or path.startswith(f"{ended}/")


def _run_of(answer: Mapping[str, Any]) -> str | None:
    """:return: The run a workflow's call started, as its answer names it."""
    payload = answer.get("payload") if answer.get("status") == "success" else None
    run = payload.get("run_id") if isinstance(payload, dict) else None
    return run if isinstance(run, str) and run else None


def _confirming(events: list[Event], answered: set[str]) -> set[str]:
    """:return: The calls a person is still to confirm (ADK's request to, unanswered)."""
    found: set[str] = set()
    for event in events:
        for call in event.get_function_calls():
            if call.name == REQUEST_CONFIRMATION and call.id not in answered:
                original = (call.args or {}).get("originalFunctionCall")
                if isinstance(original, dict) and original.get("id"):
                    found.add(str(original["id"]))
    return found


def _answering(event: Event, call: types.FunctionCall, answer: dict[str, Any]) -> Event:
    """:return: ``answer`` to ``call``, where its call is in the run: as its
    agent's tool answers, from the same agent, branch and node."""
    response = types.FunctionResponse(id=call.id, name=call.name, response=answer)
    reply = Event(
        invocation_id=event.invocation_id,
        author=event.author,
        branch=event.branch,
        content=types.Content(role="user", parts=[types.Part(function_response=response)]),
        custom_metadata={CUT_OFF_KEY: True},
    )
    reply.node_info = event.node_info.model_copy()
    reply.isolation_scope = event.isolation_scope
    return reply


def _starts_over(document: Mapping[str, Any]) -> bool:
    """:return: Whether the workflow's runs start over in a new session when
    ADK can't carry them on where they stopped (its start's ``start_over``)."""
    for node in items(document.get("nodes")):
        if isinstance(node, dict) and node.get("kind") == "start":
            return config_of(node).get("start_over") is True
    return False


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


async def _closed(what: str, closing: Awaitable[None]) -> None:
    """Waits for something the run opened to close, CLOSE_SECONDS at most: one
    that never finishes closing mustn't hold the run, and its lease, forever."""
    try:
        async with asyncio.timeout(CLOSE_SECONDS):
            await closing
    except TimeoutError:
        log.warning("adk_workflows: closing %s took more than %gs; the run goes on without it", what, CLOSE_SECONDS)


def _utc(when: datetime) -> datetime:
    """A time in UTC; a naive one is UTC already."""
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _seconds_until(due: datetime | None, now: datetime) -> int | None:
    if due is None:
        return None
    return max(0, math.ceil((due - now).total_seconds())) + DEADLINE_MARGIN


def _classified(error: Exception) -> Exception:
    """What an error that stopped a run means for it: try again later
    (:class:`TransientError`), or the run can't go on (:class:`_Refused`).
    It's named by its root: what an error ADK wraps a step's in
    (``DynamicNodeFailError``) wraps, with its HTTP status when it has one."""
    error = _root(error)
    message = str(getattr(error, "message", "") or error)[:MAX_ERROR] or type(error).__name__
    status = _status_of(error)
    kind = f"{type(error).__name__} (HTTP {status})" if status is not None else type(error).__name__
    if _transient(error):
        return TransientError(f"The run can't go on for now ({kind}): {message}")
    log.warning("adk_workflows: a run stopped: %s", message, exc_info=error)
    return _Refused(f"{kind}: {message}")


def _root(error: Exception) -> Exception:
    # The error a step raised, out of what ADK wraps it in on its way up.
    seen: set[int] = set()
    while isinstance(error, DynamicNodeFailError) and id(error) not in seen:
        seen.add(id(error))
        error = error.error
    return error


def _status_of(error: BaseException) -> int | None:
    # An HTTP status an error carries: an API client's (``status_code``), genai's
    # (``code``), or its response's.
    for found in (
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(found, int) and not isinstance(found, bool) and 100 <= found <= 599:
            return found
    return None


def _transient(error: BaseException) -> bool:
    if isinstance(error, TaskError):
        return not error.permanent
    status = _status_of(error)
    if status is not None and (status in RETRYABLE or status >= 500):
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
