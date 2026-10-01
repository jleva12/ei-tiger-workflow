"""
Logic, as the worker runs it (``forge_task_workflows.engine`` and
``nodes/basic.py``), and the hidden nodes the build adds.

- ``if`` / ``switch`` / ``match``: take a way (true / false; the case whose
  value the expression gives, as text, else default; the first rule whose
  condition holds, else otherwise) and hand on what came in. The event's route
  is the way's output ID: ``steps.<id>.output`` records it as ``{branch}``.
- ``merge``: "all" is a ``JoinNode`` that waits for every way in and hands on
  what each handed on, by step ID. "any" goes on at the first way in (each
  way in tags what it hands on with its step ID on the way, so it's the
  worker's ``{<id>: ...}`` too) and ends the others: it runs once per
  arrival, and after the first hands on nothing, by no way.
- ``loop``: goes through its list's items (None: none; one value: a list of
  it), running its body per item as an ADK dynamic node (``ctx.run_node``),
  one at a time when the body can pause, else up to its concurrency at once.
  It hands on ``{count, results}`` (what each item's body handed back) by its
  Done way. More items than it allows fail the run.
- ``end``: finishes its way with ``{outcome, result}``, its result its
  expression's, else what came in. A failed one fails the run
  (:class:`RunFailed` with the result). Unlike the worker's, a succeeded End
  can't stop other ways still running.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from google.adk import Context, Event
from google.adk.workflow import BaseNode, FunctionNode, JoinNode
from pydantic import Field

from forge_task_adk_workflows.graph.data import INPUT_NODE, PRIVATE_STATE, as_data
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.graph.factories.base import (
    BuildContext,
    failed,
    label_of,
    settings_of,
    text,
)
from forge_task_adk_workflows.graph.names import (
    BACK_NODE,
    ENDED_NODE,
    FINISH_NODE,
    STOP_NODE,
    adk_name,
)
from forge_task_workflows.document import (
    EndConfig,
    IfConfig,
    LoopConfig,
    MatchConfig,
    MergeConfig,
    SwitchConfig,
)
from forge_task_workflows.errors import StepFailed
from forge_task_workflows.expressions import as_text

#: The way out of a Merge "any" (and of the others, which don't need it).
NEXT = "next"


def if_(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, IfConfig)
    evaluator = ctx.services.evaluator

    def run(adk: Context, node_input: Any) -> Event:
        try:
            holds = evaluator.truthy(s.condition, ctx.data(adk, node_input))
        except StepFailed as error:
            raise failed(node, error) from None
        return Event(output=node_input, route="true" if holds else "false")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=_name(node), func=run)


def switch(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, SwitchConfig)
    evaluator = ctx.services.evaluator

    def run(adk: Context, node_input: Any) -> Event:
        try:
            value = as_text(evaluator.evaluate(s.value, ctx.data(adk, node_input), what="What it switches on"))
        except StepFailed as error:
            raise failed(node, error) from None
        way = next((case.id for case in s.cases if case.value == value), "default")
        return Event(output=node_input, route=way)  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=_name(node), func=run)


def match(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, MatchConfig)
    evaluator = ctx.services.evaluator

    def run(adk: Context, node_input: Any) -> Event:
        data = ctx.data(adk, node_input)
        way = "otherwise"
        for index, arm in enumerate(s.arms):
            what = f"The condition of {arm.label or f'rule {index + 1}'}"
            try:
                if evaluator.truthy(arm.condition, data, what=what):
                    way = arm.id
                    break
            except StepFailed as error:
                raise failed(node, error) from None
        return Event(output=node_input, route=way)  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=_name(node), func=run)


class MergeAll(JoinNode):
    """ADK's ``JoinNode``, handing on what each way in handed on by step ID
    (ADK's are by node name)."""

    #: Step IDs by ADK name.
    step_ids: dict[str, str] = Field(default_factory=dict)

    async def _run_impl(self, *, ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        async for event in super()._run_impl(ctx=ctx, node_input=node_input):
            if isinstance(event, Event) and isinstance(event.output, dict):
                event.output = {self.step_ids.get(name, name): as_data(value) for name, value in event.output.items()}
            yield event


def merge(node: dict[str, Any], ctx: BuildContext) -> BaseNode:
    s = settings_of(node, MergeConfig)
    if s.mode == "all":
        step_ids = {name: step for step, name in ctx.names.items()}
        step_ids[INPUT_NODE] = ctx.scope.start
        return MergeAll(name=_name(node), step_ids=step_ids)

    def run(adk: Context, node_input: Any) -> Event | None:
        # Once per run of its graph (a loop's item has its own).
        key = f"{PRIVATE_STATE}merged:{adk.invocation_id}:{adk.node_path.rpartition('@')[0]}"
        if adk.state.get(key):
            return None
        adk.state[key] = True
        return Event(output=node_input, route=NEXT)  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=_name(node), func=run)


def via(name: str, source: str) -> FunctionNode:
    """The hidden node on a way into a Merge "any": it tags what came in with
    the ID of the step it came from."""

    def run(node_input: Any) -> dict[str, Any]:
        return {source: as_data(node_input)}

    return FunctionNode(name=name, func=run)


def loop(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, LoopConfig)
    services = ctx.services
    body = ctx.bodies.get(node["id"])
    item_name = s.item_name or "item"
    limit = min(s.max_iterations, services.max_loop_items)
    at_once = 1 if body is None or body.pauses else max(1, s.concurrency)

    async def run(adk: Context, node_input: Any) -> Event:
        try:
            found = services.evaluator.evaluate(s.items, ctx.data(adk, node_input), what="Its list")
        except StepFailed as error:
            raise failed(node, error) from None
        # JSONata gives one item on its own, not in a list.
        items = [] if found is None else found if isinstance(found, list) else [found]
        if len(items) > limit:
            raise RunFailed(
                f"{label_of(node)} has {len(items)} items to go through; the most it may is {limit}",
                step=node["id"],
            )
        results: list[Any] = [None] * len(items)

        async def one(index: int) -> None:
            assert body is not None
            graph = body.build({**ctx.bindings, item_name: items[index], "index": index})
            handed = await adk.run_node(
                graph,
                node_input=items[index],
                run_id=f"item_{index}",
                use_sub_branch=True,
            )
            results[index] = as_data(handed)

        if body is not None:
            await _each(one, len(items), at_once)
        return Event(output={"count": len(items), "results": results}, route="done")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=_name(node), func=run, rerun_on_resume=True)


async def _each(one: Callable[[int], Awaitable[None]], count: int, at_once: int) -> None:
    # Every item, at most at_once at a time: a sliding window, as ADK's own
    # _ParallelWorker. The first failure stops the rest, and is the loop's.
    if at_once <= 1:
        for index in range(count):
            await one(index)
        return
    running: set[asyncio.Future[None]] = set()
    started = 0
    try:
        while started < count or running:
            while started < count and len(running) < at_once:
                running.add(asyncio.ensure_future(one(started)))
                started += 1
            done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            # Every failure retrieved, the first raised.
            errors = [task.exception() for task in done]
            error = next((error for error in errors if error is not None), None)
            if error is not None:
                raise error
    finally:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


def end(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, EndConfig)
    evaluator = ctx.services.evaluator

    def run(adk: Context, node_input: Any) -> dict[str, Any]:
        if s.result.strip():
            try:
                result = evaluator.evaluate(s.result, ctx.data(adk, node_input), what="Its result")
            except StepFailed as error:
                raise failed(node, error) from None
        else:
            result = as_data(node_input)
        if s.outcome == "failed":
            said = as_text(result)
            raise RunFailed(
                f"{label_of(node)} failed the run" + (f": {said}" if said else ""),
                result=result,
                step=node["id"],
            )
        return {"outcome": "succeeded", "result": result}

    return FunctionNode(name=_name(node), func=run)


def finish(*, top: bool) -> FunctionNode:
    """
    The hidden node every ending of an agent's graph leads to: ADK takes one
    ending with an output. It hands on ``{outcome, result}``, the run's; a
    saved agent hands on its result, as the worker's Run workflow step does.
    """

    def run(node_input: Any) -> Any:
        if top or not isinstance(node_input, dict):
            return node_input
        return node_input.get("result")

    return FunctionNode(name=FINISH_NODE, func=run)


def ended() -> FunctionNode:
    """The hidden node the endings that aren't End steps lead to: their run
    succeeded, with what they handed on as its result."""

    def run(node_input: Any) -> dict[str, Any]:
        return {"outcome": "succeeded", "result": as_data(node_input)}

    return FunctionNode(name=ENDED_NODE, func=run)


def back() -> FunctionNode:
    """The hidden node a loop body's ways back lead to: what came back is the
    item's result."""

    def run(node_input: Any) -> Any:
        return node_input

    return FunctionNode(name=BACK_NODE, func=run)


def stop() -> FunctionNode:
    """The hidden node a loop body's other endings lead to: nothing comes back
    from them."""

    def run(node_input: Any) -> None:
        return None

    return FunctionNode(name=STOP_NODE, func=run)


def _name(node: dict[str, Any]) -> str:
    return adk_name(text(node.get("name")))
