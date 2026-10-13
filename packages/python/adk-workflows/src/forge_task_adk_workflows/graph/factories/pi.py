"""
Pi agent: pi (pi.dev), the coding agent, run in a folder of the run's own
(``forge_pi_agent``), one pi process per step it takes.

- **Folder.** ``folder`` is a template: a folder in the run's folder
  (``state.workspace``, ``workspaces``), relative to it (``backend``,
  ``{{ item.name }}``) or starting with it (``{{ state.workspace }}/backend``);
  empty is the run's folder itself, so Pi agents of one run can each work in
  a folder of their own. It's made when it isn't there. One that leads outside
  the run's folder (``..``, an absolute path elsewhere, a link) fails the step,
  and so does a run without a folder (its start doesn't allow a file system).
- **Prompt.** ``prompt`` (a template) is what pi is asked; empty, what the step
  is handed, as text. ``instruction`` (a template) is added to pi's system
  prompt.
- **Tools, model, thinking.** pi's built-in tools the step allows (none is
  none); one of the worker's models (``model``, as an LLM agent's; the default
  when it names none it has), with its credential from the model provider
  configuration (an OAuth2 token renewed as pi works); its thinking level, or
  pi's default.
- **Output schema.** With one, pi is told to answer with JSON that fits it;
  its answer is parsed and held to it, and an answer that doesn't fit is sent
  back once, saying why. Still not, the step fails.
- **Hands on** ``{answer, tool_calls, usage, folder}``: its answer (text, or
  what fits its output schema), its tool calls (``{tool, args, is_error}``),
  its tokens and cost (``{input, cached, output, cost}``, cost null when the
  model has no price) and the folder it worked in.
- **Usage.** Each of pi's model and tool calls (``pi:<tool>``) is recorded as
  the run's, where the worker records usage (``forge_common.adk.usage``).

The step fails when the worker doesn't run Pi agents (``pi_agent`` off), pi
fails (a model error it gave up on, pi exiting) or it takes longer than its
timeout (at most the worker's ``pi_max_seconds``): pi is then stopped, with
the commands it started.
"""

import asyncio
import functools
import json
import logging
from pathlib import Path
from typing import Any

from forge_common.adk.models import ProviderModels
from forge_common.adk.usage import Attribution, Tokens, UsageCall, cost_of, current_sink
from forge_common.model_provider import ResolvedModel
from forge_pi_agent import PiAgent, PiError, PiModel, PiOptions, PiResult, PiUsage, json_answer
from google.adk import Context
from google.adk.workflow import FunctionNode
from pydantic import ValidationError

from forge_task_adk_workflows.graph.data import as_data
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.graph.factories.base import (
    BuildContext,
    config_of,
    failed,
    label_of,
    settings_of,
    text,
)
from forge_task_adk_workflows.graph.names import adk_name
from forge_task_adk_workflows.graph.schemas import held_to, problems, to_model
from forge_task_adk_workflows.graph.services import RunServices, model_id
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.expressions import as_text
from forge_task_adk_workflows.support.step_settings import PiAgentConfig

log = logging.getLogger(__name__)

#: Answers that don't fit the output schema sent back to pi, before the step fails.
FIX_ATTEMPTS = 1
#: What pi's commands find in their environment: git never waits for a password.
PI_ENV = {"GIT_TERMINAL_PROMPT": "0"}
PI_OFF = (
    "Pi agents don't run on this worker: set HYBRID_ADK_WORKFLOWS__PI_AGENT=local "
    "where pi is installed (npm install -g @earendil-works/pi-coding-agent)"
)


def folder_in(workspace: str | Path, where: str) -> Path:
    """
    :param workspace: The run's folder.
    :param where: A folder in it: relative to it, or starting with it; empty for it.
    :return: The folder, links resolved.
    :raises ValueError: When it isn't in the run's folder.
    """
    root = Path(workspace).resolve()
    asked = Path(where) if where else root
    found = (asked if asked.is_absolute() else root / asked).resolve()
    if not found.is_relative_to(root):
        raise ValueError(f"its folder ({where}) isn't in the run's folder ({root})")
    return found


def schema_note(schema: dict[str, Any]) -> str:
    """:return: What pi's prompt adds when its answer is held to a JSON Schema."""
    return (
        "When you're done, answer with only a JSON value that fits this JSON Schema, "
        f"and nothing else:\n```json\n{json.dumps(schema, indent=2)}\n```"
    )


def pi_agent(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, PiAgentConfig)
    services = ctx.services
    evaluator = services.evaluator
    label = label_of(node)
    name = adk_name(text(node.get("name")))
    schema = s.output_schema or None
    answer_type = to_model(schema) if schema else None
    chosen = model_id(config_of(node))

    def fail(why: str) -> RunFailed:
        return RunFailed(f"{label} failed: {why}", step=node.get("id"))

    async def run(adk: Context, node_input: Any) -> dict[str, Any]:
        if services.pi_command is None:
            raise fail(PI_OFF)
        models = services.model
        if not isinstance(models, ProviderModels):
            raise fail(
                "Pi agents run on the worker's model provider configuration "
                "(HYBRID_ADK_WORKFLOWS__MODEL_PROVIDER_CONFIG), and it has none"
            )
        if services.workspace is None:
            raise fail("the run has no folder to work in: turn on Allow file system on the workflow's start")
        data = ctx.data(adk, node_input)
        try:
            where = (await evaluator.render(s.folder, data, what="Its folder")).strip()
            asked = (await evaluator.render(s.prompt, data, what="Its prompt")).strip()
            instruction = await evaluator.render(s.instruction, data, what="Its instruction")
        except StepFailed as error:
            raise failed(node, error) from None
        message = asked or as_text(as_data(node_input)).strip()
        if not message:
            raise fail("it has nothing to ask pi: give it a prompt, or hand it something")
        try:
            folder = folder_in(services.workspace, where)
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        except (ValueError, OSError) as error:
            raise fail(str(error)) from None
        model = models.resolve(chosen)
        options = PiOptions(
            folder=folder,
            model=PiModel(model, functools.partial(models.credential, model)),
            tools=tuple(s.tools),
            thinking=s.thinking_level or None,
            instruction=instruction,
            context_files=s.context_files,
            command=services.pi_command,
            env=PI_ENV,
        )
        seconds = min(s.timeout_seconds, services.pi_max_seconds)
        results: list[PiResult] = []
        try:
            async with asyncio.timeout(seconds), PiAgent(options) as pi:
                result = await pi.prompt(f"{message}\n\n{schema_note(schema)}" if schema else message)
                results.append(result)
                answer: Any = result.text
                if answer_type is not None:
                    answer, misfit = _fit(answer_type, result.text)
                    for _ in range(FIX_ATTEMPTS):
                        if misfit is None:
                            break
                        result = await pi.prompt(
                            f"Your answer doesn't fit the JSON Schema: {misfit}. "
                            "Answer again with only the JSON that fits it."
                        )
                        results.append(result)
                        answer, misfit = _fit(answer_type, result.text)
                    if misfit is not None:
                        raise fail(f"pi's answer doesn't fit its output schema: {misfit}")
        except TimeoutError:
            raise fail(f"pi took longer than {seconds:g} seconds; it was stopped") from None
        except PiError as error:
            raise fail(str(error)) from None
        finally:
            await _record(results, adk, services, model, author=name)
        usage = PiUsage()
        for each in results:
            usage += each.usage
        tokens = _tokens(usage)
        return {
            "answer": answer,
            "tool_calls": [
                {"tool": call.tool, "args": call.args, "is_error": call.is_error}
                for each in results
                for call in each.tool_calls
            ],
            "usage": {
                "input": tokens.input,
                "cached": tokens.cached,
                "output": tokens.output,
                "cost": cost_of(tokens, model.model.cost),
            },
            "folder": str(folder),
        }

    return FunctionNode(name=name, func=run)


def _fit(answer_type: Any, answer: str) -> tuple[Any, str | None]:
    """:return: The answer held to its output schema, or None and why it doesn't fit."""
    try:
        value = json_answer(answer)
    except ValueError:
        return None, "it isn't JSON"
    try:
        return held_to(answer_type, value), None
    except ValidationError as error:
        return None, problems(error, "answer")


def _tokens(usage: PiUsage) -> Tokens:
    """pi's usage as Forge counts tokens: input is the whole prompt, cached and written included."""
    whole = usage.input + usage.cache_read + usage.cache_write
    return Tokens(
        input=whole,
        cached=usage.cache_read,
        output=usage.output,
        thinking=min(usage.reasoning, usage.output),
        cache_write=usage.cache_write,
    )


async def _record(
    results: list[PiResult], adk: Context, services: RunServices, model: ResolvedModel, *, author: str
) -> None:
    """Record pi's model and tool calls as the run's (``forge_common.adk.usage``)."""
    sink = current_sink()
    attribution = services.attribution
    if sink is None or not isinstance(attribution, Attribution):
        return
    session_id = adk.session.id
    common: dict[str, Any] = {
        "attribution": attribution,
        "author": author,
        "invocation_id": adk.invocation_id,
        "session_id": session_id,
        "user_id": attribution.user_id or adk.session.user_id,
        "run_id": attribution.run_id or session_id,
    }
    calls: list[UsageCall] = []
    for result in results:
        for call in result.model_calls:
            tokens = _tokens(call.usage)
            calls.append(
                UsageCall(
                    call="model",
                    name=model.ref,
                    at=services.clock(),
                    input_tokens=tokens.input,
                    cached_tokens=tokens.cached,
                    output_tokens=tokens.output,
                    thinking_tokens=tokens.thinking,
                    cost=cost_of(tokens, model.model.cost),
                    failed=call.stop_reason == "error",
                    **common,
                )
            )
        calls.extend(
            UsageCall(call="tool", name=f"pi:{tool.tool}", at=services.clock(), failed=tool.is_error, **common)
            for tool in result.tool_calls
        )
    for call in calls:
        try:
            await sink(call)
        except Exception:  # Recording never fails a run.
            log.warning("adk_workflows: a pi %s call of run %s wasn't recorded", call.call, call.run_id, exc_info=True)
