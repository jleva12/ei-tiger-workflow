"""The engine: runs a workflow's graph step by step, as one tracked run of the
async worker.

It walks the graph from the start step: each step runs, takes one of its ways
out, and the steps that way leads to run next, in order (a way that leads to
several steps runs each of them). A Merge waits for every way still coming to
it ("all") or goes on at the first ("any"); a Loop runs its body once per
item, several at once when it's allowed to and nothing in the body waits; an
End finishes the run. A connection that comes back to an earlier step runs it
again (a polling loop), up to a limit.

**Resuming.** After every step, what it did (its way out, its output, its
error) is kept with the run and checkpointed. The run's state is only ever
added to, and the walk is deterministic, so when a run starts again (after a
wait, a decision, a retry) the engine walks the graph from the start, replays
every step it already finished from what it kept, and carries on at the first
step that hasn't. A step is never run twice: an agent isn't paid for again, a
child workflow isn't started again, a POST isn't sent again.

**Waiting.** A step that waits (an approval, a delay, or another workflow
still running) asks the job's control to wait, which ends the
attempt and lets the worker go; the run starts again when it's time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NoReturn

from forge_task_workflows.config import WorkflowsSettings
from forge_task_workflows.document import ERROR_OUTPUT, PASS_THROUGH, WAITING, Node, Workflow
from forge_task_workflows.errors import StepFailed, WorkflowFailed
from forge_task_workflows.expressions import Evaluator, plain
from forge_tasks.control import JobControl

log = logging.getLogger(__name__)

STATE_KEY = "workflow"
Frame = tuple[tuple[str, int], ...]  # the loops a step runs in: (loop step, item index), outermost first
# Notes on the run's activity, at most: a loop of thousands of items shouldn't bury the rest.
MAX_NOTES = 400


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Token:
    """A way into a step: where the walk goes next, and what it carries."""

    node: str
    frame: Frame
    previous: Any
    via: str | None = None


@dataclass
class Outcome:
    """What a step did: the way out it took, its output, and its error when it failed its way."""

    branch: str
    output: Any = None
    error: dict[str, Any] | None = None


@dataclass
class RunInfo:
    """Whose run it is and what it runs: the job's payload, as the admin API sent it."""

    organization_id: str
    workflow_id: str
    workflow_name: str
    revision: int
    run_as: str
    run_as_name: str
    depth: int
    input: Any


class _Finished(Exception):
    def __init__(self, outcome: str, result: Any, step: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.result = result
        self.step = step


@dataclass
class StepRun:
    """What a step's code is given: the step, what it can read, the services,
    and a place to keep its own progress (a workflow it started, the
    conversation an agent had) between attempts."""

    engine: Engine
    node: Node
    token: Token
    key: str

    @property
    def settings(self) -> Any:
        return self.node.settings

    @property
    def services(self) -> Any:
        return self.engine.services

    @property
    def run(self) -> RunInfo:
        return self.engine.info

    @property
    def control(self) -> JobControl:
        return self.engine.control

    @property
    def data(self) -> dict[str, Any]:
        """What the step's expressions read."""
        return self.engine.data_for(self.token)

    def evaluate(self, expression: str, *, what: str = "Its expression") -> Any:
        return self.engine.evaluator.evaluate(expression, self.data, what=what)

    def render(self, template: str, *, what: str = "Its text") -> str:
        return self.engine.evaluator.render(template, self.data, what=what)

    def truthy(self, expression: str, *, what: str = "Its condition") -> bool:
        return self.engine.evaluator.truthy(expression, self.data, what=what)

    @property
    def label(self) -> str:
        """How the run's activity names this step (and, in a loop, the item it's on)."""
        return self.engine.label_of(self.node, self.token.frame)

    @property
    def progress(self) -> dict[str, Any]:
        """What this step kept of its own work so far (empty the first time)."""
        return self.engine.progress(self.key)

    async def keep(self, **progress: Any) -> None:
        """Keep some of the step's work, durably: a restart carries on from it."""
        await self.engine.keep_progress(self.key, progress, where=self.label)

    async def wait_until(self, when: datetime, *, reason: str) -> NoReturn:
        """Let the worker go until ``when``; the step runs again then, with what it kept."""
        await self.engine.note(f"{self.label} waits until {when.isoformat(timespec='seconds')}: {reason}")
        await self.control.wait_until(when, reason=f"{self.label}: {reason}")
        raise AssertionError("a wait always ends the attempt")  # pragma: no cover

    async def note(self, message: str, **attributes: Any) -> None:
        await self.engine.note(f"{self.label}: {message}", **attributes)


Executor = Callable[[StepRun], Awaitable[Outcome]]


@dataclass
class _Merge:
    arrivals: dict[str, Any] = field(default_factory=dict)
    fired: bool = False


class Engine:
    """
    Engine class is responsible for managing and executing workflows. It tracks the state of the workflow run,
    executes various steps, handles merges, and provides utilities for monitoring and controlling progress.

    The class utilizes an asynchronous approach for saving state, noting progress, and driving the workflow
    execution. Through its state management capabilities, Engine ensures that workflows can be paused, resumed,
    and analyzed for progress at any point. It provides mechanisms for node-specific execution and supports
    complex decision-making through merges and branching.

    :ivar workflow: The workflow object being managed and executed by this engine.
    :type workflow: Workflow
    :ivar info: Contains metadata and information associated with the current workflow run.
    :type info: RunInfo
    :ivar control: Provides job control utilities for state management and checkpointing.
    :type control: JobControl
    :ivar services: External services required for the execution of the workflow.
    :type services: Any
    :ivar executors: A dictionary mapping node IDs to their respective executor instances.
    :type executors: dict[str, Executor]
    :ivar settings: Configuration and settings governing workflow behavior and performance.
    :type settings: WorkflowsSettings
    :ivar evaluator: An instance of Evaluator used for expression evaluation during workflow execution.
    :type evaluator: Evaluator
    :ivar state: Tracks the current state of the workflow run, including records, progress, count, and completion status.
    :type state: dict[str, Any]
    """

    def __init__(
        self,
        workflow: Workflow,
        info: RunInfo,
        *,
        control: JobControl,
        services: Any,
        executors: dict[str, Executor],
        settings: WorkflowsSettings,
    ) -> None:
        self.workflow = workflow
        self.info = info
        self.control = control
        self.services = services
        self.executors = executors
        self.settings = settings
        self.evaluator = Evaluator(settings.expression_timeout_ms, settings.expression_depth)
        stored = control.load(STATE_KEY) or {}
        self.state: dict[str, Any] = {
            "records": stored.get("records", {}),
            "progress": stored.get("progress", {}),
            "executed": stored.get("executed", 0),
            "notes": stored.get("notes", 0),
            "finished": stored.get("finished"),
        }
        self._visits: dict[tuple[Frame, str], int] = {}
        self._latest: dict[tuple[Frame, str], str] = {}
        self._items: dict[Frame, tuple[str, Any, int]] = {}
        self._last_previous: Any = None
        self._save_lock = asyncio.Lock()

    # ------------------------------------------------------------------ state

    def progress(self, key: str) -> dict[str, Any]:
        return dict(self.state["progress"].get(key, {}))

    async def keep_progress(self, key: str, progress: dict[str, Any], *, where: str = "") -> None:
        self.state["progress"][key] = {**self.state["progress"].get(key, {}), **plain(progress)}
        await self._save(where)

    async def _save(self, where: str = "") -> None:
        """Checkpoint the run's state; ``where`` names the step it's at, on the run's activity."""
        async with self._save_lock:
            self.control.keep(STATE_KEY, self.state)
            await self.control.checkpoint(where[:500])

    def label_of(self, node: Node, frame: Frame) -> str:
        """A step as the run's activity names it: its name and ID, and in a
        loop's body, which item it's on (``Read the file (read), item 2 of Files``)."""
        if not frame:
            return node.label
        loop_id, index = frame[-1]
        loop = self.workflow.by_id.get(loop_id)
        return f"{node.label}, item {index + 1} of {(loop.name if loop else '') or loop_id}"

    async def note(self, message: str, **attributes: Any) -> None:
        if self.state["notes"] >= MAX_NOTES:
            return
        self.state["notes"] += 1
        try:
            await self.control.note(message[:500], **attributes)
        except Exception:
            log.warning("couldn't note on the run: %s", message, exc_info=True)

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _frame_path(frame: Frame) -> str:
        return "".join(f"{loop}[{index}]/" for loop, index in frame)

    def data_for(self, token: Token) -> dict[str, Any]:
        """What a step reached by ``token`` reads: the run's input, what every
        step it can see handed on (in its loop's item first), ``previous``, and
        the items of the loops it's in."""
        steps: dict[str, dict[str, Any]] = {}
        frames = [token.frame[:depth] for depth in range(len(token.frame) + 1)]
        for frame in frames:  # outermost first: an inner visit wins
            for (at, node_id), key in self._latest.items():
                if at != frame:
                    continue
                record = self.state["records"][key]
                entry: dict[str, Any] = {"output": record.get("output")}
                if record.get("error") is not None:
                    entry["error"] = record["error"]
                steps[node_id] = entry
        data: dict[str, Any] = {"input": self.info.input, "steps": steps, "previous": token.previous}
        for frame in frames[1:]:
            name, item, index = self._items[frame]
            data[name] = item
            data["index"] = index
        return data

    # ------------------------------------------------------------------ running

    async def run(self) -> dict[str, Any]:
        """Run to the end (or to the next wait, which raises); what the run finished with."""
        if self.state["finished"] is not None:
            return self.state["finished"]
        start = Token(self.workflow.entry or "", (), None)
        try:
            await self._drive([start], (), None)
            finished = {"outcome": "succeeded", "result": plain(self._last_previous), "step": None}
        except _Finished as end:
            finished = {"outcome": end.outcome, "result": end.result, "step": end.step}
        self.state["finished"] = finished
        await self._save(f"The run {finished['outcome']}")
        await self.note(f"The run {finished['outcome']}")
        return finished

    async def _drive(self, tokens: list[Token], frame: Frame, stop_at: str | None) -> list[Token]:
        """Walk from ``tokens`` until nothing is left to run; returns the tokens
        that came back to ``stop_at`` (a loop's body ending an item)."""
        queue: deque[Token] = deque(tokens)
        back: list[Token] = []
        merges: dict[str, _Merge] = {}
        while True:
            if not queue:
                # Nothing left to run but merges nothing more can reach: they go on now.
                if not await self._fire_ready(merges, queue, frame):
                    break
                continue
            token = queue.popleft()
            if stop_at is not None and token.node == stop_at:
                back.append(token)
                continue
            node = self.workflow.by_id[token.node]
            if node.kind == "merge":
                await self._arrive(node, token, merges, queue, frame)
                continue
            outcome, handed_on = await self._visit(node, token)
            self._last_previous = handed_on
            for target in self.workflow.targets(node.id, outcome.branch):
                queue.append(Token(target, frame, handed_on, node.id))
            await self._fire_ready(merges, queue, frame)
        return back

    # ------------------------------------------------------------------ merges

    def _waiting_on(self, merge_id: str, queue: deque[Token]) -> bool:
        return any(t.node == merge_id or self.workflow.can_reach(t.node, merge_id) for t in queue)

    async def _fire_ready(self, merges: dict[str, _Merge], queue: deque[Token], frame: Frame) -> bool:
        """Every "all" merge nothing more can reach goes on; whether one did."""
        fired = False
        for merge_id, merge in list(merges.items()):
            if merge.arrivals and not merge.fired and not self._waiting_on(merge_id, queue):
                await self._fire(self.workflow.by_id[merge_id], merge, queue, frame)
                fired = True
        return fired

    async def _arrive(
        self, node: Node, token: Token, merges: dict[str, _Merge], queue: deque[Token], frame: Frame
    ) -> None:
        via = token.via or f"way{len(merges.get(node.id, _Merge()).arrivals)}"
        merge = merges.setdefault(node.id, _Merge())
        if merge.fired:
            if node.settings.mode == "any" and via not in merge.arrivals:
                return  # the other ways of an "any" merge: it went on at the first
            # The same way again: a connection came back through it, a new visit.
            merge = merges[node.id] = _Merge()
        merge.arrivals[via] = token.previous
        if node.settings.mode == "any" or not self._waiting_on(node.id, queue):
            await self._fire(node, merge, queue, frame)

    async def _fire(self, node: Node, merge: _Merge, queue: deque[Token], frame: Frame) -> None:
        merge.fired = True
        token = Token(node.id, frame, None)
        outcome, handed_on = await self._visit(node, token, arrivals=dict(merge.arrivals))
        self._last_previous = handed_on
        for target in self.workflow.targets(node.id, outcome.branch):
            queue.append(Token(target, frame, handed_on, node.id))

    # ------------------------------------------------------------------ visiting a step

    async def _visit(self, node: Node, token: Token, *, arrivals: dict[str, Any] | None = None) -> tuple[Outcome, Any]:
        frame = token.frame
        visit = self._visits.get((frame, node.id), 0) + 1
        self._visits[(frame, node.id)] = visit
        if visit > self.settings.max_visits_per_step:
            raise WorkflowFailed(
                f"{node.label} ran {self.settings.max_visits_per_step} times in one run: "
                "a connection that comes back to it never stops"
            )
        key = f"{self._frame_path(frame)}{node.id}#{visit}"
        record = self.state["records"].get(key)
        if record is not None:
            outcome = Outcome(record["branch"], record.get("output"), record.get("error"))
            if record["branch"] == "__end__":
                self._latest[(frame, node.id)] = key
                raise _Finished(record["output"]["outcome"], record["output"]["result"], node.id)
        else:
            self.state["executed"] += 1
            if self.state["executed"] > self.settings.max_steps:
                raise WorkflowFailed(f"The run ran {self.settings.max_steps} steps, the most one run may")
            outcome = await self._execute(node, token, key, arrivals)
            await self._record(node, key, outcome, self.label_of(node, frame))
        self._latest[(frame, node.id)] = key
        handed_on = token.previous if node.kind in PASS_THROUGH else outcome.output
        return outcome, handed_on

    async def _execute(self, node: Node, token: Token, key: str, arrivals: dict[str, Any] | None) -> Outcome:
        if node.kind == "merge":
            return Outcome("next", plain(arrivals or {}))
        if node.kind == "loop":
            return await self._loop(node, token, key)
        if node.kind == "end":
            await self._end(node, token, key)
        executor = self.executors.get(node.kind)
        if executor is None:
            raise WorkflowFailed(f"{node.label} is a {node.kind!r}, which this worker doesn't run")
        step = StepRun(self, node, token, key)
        try:
            outcome = await executor(step)
        except StepFailed as failed:
            way = ERROR_OUTPUT.get(node.kind)
            if way is not None and self.workflow.targets(node.id, way):
                await self.note(f"{step.label} failed, and takes its {way} way: {failed.message}")
                return Outcome(way, None, failed.as_error())
            raise WorkflowFailed(f"{node.label} failed: {failed.message}") from failed
        return outcome

    async def _record(self, node: Node, key: str, outcome: Outcome, label: str) -> None:
        output = plain(outcome.output)
        size = len(json.dumps(output))
        if size > self.settings.max_output_bytes:
            raise WorkflowFailed(
                f"{node.label} handed on {size:,} bytes; the most a step's output may be is "
                f"{self.settings.max_output_bytes:,}"
            )
        self.state["records"][key] = {
            "node": node.id,
            "branch": outcome.branch,
            "output": output,
            "error": outcome.error,
            "at": utcnow().isoformat(),
        }
        self.state["progress"].pop(key, None)
        await self._save(label)
        if node.kind not in PASS_THROUGH and node.kind != "merge":
            await self.note(f"{label} finished" + (f", {outcome.branch}" if outcome.branch != "next" else ""))

    async def _end(self, node: Node, token: Token, key: str) -> NoReturn:
        settings = node.settings
        step = StepRun(self, node, token, key)
        try:
            result = step.evaluate(settings.result, what="Its result") if settings.result.strip() else token.previous
        except StepFailed as failed:
            raise WorkflowFailed(f"{node.label} failed: {failed.message}") from failed
        finished = {"outcome": settings.outcome, "result": plain(result)}
        self.state["records"][key] = {
            "node": node.id,
            "branch": "__end__",
            "output": finished,
            "error": None,
            "at": utcnow().isoformat(),
        }
        self._latest[(token.frame, node.id)] = key
        await self._save(step.label)
        raise _Finished(settings.outcome, finished["result"], node.id)

    # ------------------------------------------------------------------ loops

    async def _loop(self, node: Node, token: Token, key: str) -> Outcome:
        settings = node.settings
        step = StepRun(self, node, token, key)
        try:
            items = step.evaluate(settings.items, what="Its list")
        except StepFailed as failed:
            raise WorkflowFailed(f"{node.label} failed: {failed.message}") from failed
        if items is None:
            items = []
        elif not isinstance(items, list):
            items = [items]  # JSONata gives one item on its own, not in a list
        limit = min(settings.max_iterations, self.settings.max_loop_items)
        if len(items) > limit:
            raise WorkflowFailed(f"{node.label} has {len(items)} items to go through; the most it may is {limit}")
        body = self.workflow.body_of(node.id)
        waits = any(self.workflow.by_id[n].kind in WAITING for n in body)
        at_once = 1 if waits else max(1, settings.concurrency)
        results: list[Any] = [None] * len(items)
        name = settings.item_name or "item"

        async def one(index: int) -> None:
            frame = (*token.frame, (node.id, index))
            self._items[frame] = (name, items[index], index)
            starts = [Token(t, frame, items[index], node.id) for t in self.workflow.targets(node.id, "each")]
            back = await self._drive(starts, frame, stop_at=node.id)
            results[index] = plain(back[-1].previous) if back else None

        if at_once == 1:
            for index in range(len(items)):
                await one(index)
        else:
            for start in range(0, len(items), at_once):
                async with asyncio.TaskGroup() as group:
                    for index in range(start, min(start + at_once, len(items))):
                        group.create_task(one(index))
        return Outcome("done", {"count": len(items), "results": results})
