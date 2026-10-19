"""The one executor: any helper agent, from its configuration and its kit.

:class:`Helpers` is the configuration checked over the kits and toolsets it
names (a name that doesn't exist stops the API at startup). :func:`perform`
runs one helper on its input's message: its ADK agent is built for the run
(its instruction, its toolsets' tools for the organization, its model and
thinking level), run once on an in-memory :class:`~google.adk.runners.Runner`
(each run is its own conversation, never kept), and its outcome given to its
kit's ``finish``. Its model calls count as the assistant's for the
organization (:class:`~forge_common.adk.usage.UsagePlugin`).

Every way it can fail ends in a :class:`.kit.HelperError` the person can
read: never raw internals.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any

from forge_common.adk.usage import (
    MODEL_KEY,
    Attribution,
    PriceOf,
    UsagePlugin,
    UsageSink,
)
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from forge_admin.helpers.config import HelperConfig, HelperSpec
from forge_admin.helpers.kit import (
    Finished,
    HelperError,
    HelperRun,
    HelperToolset,
    Kit,
    Outcome,
)

logger = logging.getLogger(__name__)

# A toolset's grant: its tools' names, or None for all of them.
Grant = frozenset[str] | None


@dataclass(frozen=True)
class Helper:
    """One helper: its configuration, its kit, and its toolsets with their grants."""

    spec: HelperSpec
    kit: Kit[Any]
    toolsets: tuple[tuple[HelperToolset, Grant], ...] = ()

    @property
    def name(self) -> str:
        return self.spec.name

    def agent(self, run: HelperRun, model: BaseLlm) -> LlmAgent:
        """Its ADK agent for one run."""
        tools = [
            tool
            for toolset, grant in self.toolsets
            for tool in toolset.build(run)
            if grant is None or tool.name in grant
        ]
        return LlmAgent(
            name=self.spec.name,
            description=self.spec.description,
            model=model,
            instruction=_as_written(self.spec.instruction),
            before_model_callback=_choose(
                model, self.spec.model, self.spec.thinking_level
            ),
            tools=list(tools),
            # It does its one job: no transfers, no conversation of its own.
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )


class Helpers:
    """
    The helper agents: the configuration, over the kits and toolsets it can name.

    :param config: The helpers.
    :param kits: Every kit the configuration can name.
    :param toolsets: Every toolset it can name.
    :param models: The models it may name; None not to check.
    :raises ValueError: It names a kit, toolset, tool or model that doesn't
        exist, or a hand-in tool the helper isn't given.
    """

    def __init__(
        self,
        config: HelperConfig,
        kits: Iterable[Kit[Any]],
        toolsets: Iterable[HelperToolset],
        *,
        models: Iterable[str] | None = None,
    ) -> None:
        self.config = config
        self.kits = {kit.name: kit for kit in kits}
        self.toolsets = {toolset.name: toolset for toolset in toolsets}
        _check(
            config, self.kits, self.toolsets, None if models is None else set(models)
        )
        self._helpers = {
            spec.name: Helper(
                spec,
                self.kits[spec.kit_name],
                tuple(
                    (
                        self.toolsets[ref.name],
                        None if ref.tools is None else frozenset(ref.tools),
                    )
                    for ref in spec.toolsets
                ),
            )
            for spec in config.helpers
        }

    def get(self, name: str) -> Helper | None:
        return self._helpers.get(name)

    def __iter__(self) -> Any:
        return iter(self._helpers.values())


async def execute(
    helper: Helper,
    run: HelperRun,
    *,
    message: str,
    model: BaseLlm,
    attribution: Attribution | None = None,
    usage: UsageSink | None = None,
    price: PriceOf | None = None,
) -> Outcome:
    """
    Runs a helper once on ``message``.

    :param model: What it runs on: the assistant's models, or a test's.
    :param attribution: Whom its model calls count for; with ``usage``.
    :param usage: Where its model calls are recorded; None records nothing.
    :param price: Prices its calls.
    :return: Its last answer, and what it handed in.
    :raises HelperError: It stopped, or ended without what it owes: an
        answer, or a call of its hand-in tool.
    """
    spec = helper.spec
    plugins = (
        [UsagePlugin(usage, attribution, price=price)]
        if usage is not None and attribution is not None
        else []
    )
    sessions = InMemorySessionService()
    runner = Runner(
        app=App(name=spec.name, root_agent=helper.agent(run, model), plugins=plugins),
        session_service=sessions,
    )
    session = await sessions.create_session(
        app_name=spec.name, user_id=run.user_id, session_id=uuid.uuid4().hex
    )
    new_message = types.Content(role="user", parts=[types.Part(text=message)])
    text, model_name, stopped = "", "", ""
    try:
        async with aclosing(
            runner.run_async(
                user_id=run.user_id, session_id=session.id, new_message=new_message
            )
        ) as events:
            async for event in events:
                if event.error_code:
                    # A model that raised raises after its event: perform says
                    # why. One that stopped (safety, its token limit) only
                    # says so here.
                    stopped = event.error_code
                    continue
                if event.partial or event.author != spec.name:
                    continue
                model_name = (event.custom_metadata or {}).get(
                    MODEL_KEY, ""
                ) or model_name
                written = "".join(
                    part.text
                    for part in (event.content.parts if event.content else None) or ()
                    if part.text and not part.thought
                )
                if written:
                    text = written
    finally:
        await runner.close()
    handed_in = run.handed_in[-1] if run.handed_in else None
    owed = handed_in is None if spec.hand_in else not text.strip()
    if owed:
        if stopped:
            raise HelperError(
                f"The model stopped before {spec.called} finished ({stopped[:60]}); "
                "try again."
            )
        raise HelperError(
            f"{spec.subject} finished without handing anything in; try again, "
            "or describe it more."
            if spec.hand_in
            else f"{spec.subject} answered nothing; try again."
        )
    return Outcome(
        text=text.strip(),
        handed_in=handed_in,
        model=model_name or getattr(model, "model", "") or "",
    )


async def perform(
    helper: Helper,
    run: HelperRun,
    given: BaseModel,
    *,
    message: str,
    model: BaseLlm,
    attribution: Attribution | None = None,
    usage: UsageSink | None = None,
    price: PriceOf | None = None,
) -> Finished:
    """
    Runs a helper (:func:`execute`), within its time, and has its kit finish.

    :param given: Its input, as its kit read it.
    :param message: What its kit prepared from it.
    :raises HelperError: Any way it failed, said for the person.
    """
    spec = helper.spec
    try:
        async with asyncio.timeout(spec.seconds):
            outcome = await execute(
                helper,
                run,
                message=message,
                model=model,
                attribution=attribution,
                usage=usage,
                price=price,
            )
            finished = await helper.kit.finish(run, given, outcome)
            return replace(finished, model=outcome.model)
    except HelperError:
        raise
    except Exception as error:
        raise describe(spec, error, run) from None


def describe(spec: HelperSpec, error: Exception, run: HelperRun) -> HelperError:
    """What went wrong, for the person: never raw internals."""
    if isinstance(error, TimeoutError):
        return HelperError(
            f"{spec.subject} took too long; try again, or ask for less.",
            status_code=504,
        )
    from forge_common.model_provider import ModelProviderAuthError
    from google.genai import errors as genai_errors

    if isinstance(error, genai_errors.APIError):
        return HelperError(
            f"The model refused the request ({error.code} {error.status}): {error.message}"
        )
    if isinstance(error, ModelProviderAuthError):
        # Its message holds no credentials.
        return HelperError(
            f"{spec.subject} couldn't sign in to the model's provider: {error}"
        )
    if type(error).__module__.startswith("litellm"):
        return HelperError(f"The model refused the request: {str(error)[:500]}")
    logger.error(
        "The %s helper failed in organization %s",
        spec.name,
        run.organization_id,
        exc_info=error,
    )
    return HelperError(f"{spec.subject} ran into a problem. Try again.")


def _as_written(instruction: str) -> Callable[[ReadonlyContext], str]:
    # A provider, not a string: ADK would read its {{ }} and braces as state.
    def provide(_context: ReadonlyContext) -> str:
        return instruction

    return provide


def _choose(
    model: BaseLlm, name: str | None, thinking_level: str | None
) -> Callable[[CallbackContext, LlmRequest], None]:
    """Runs each call on its model, thinking as long as it says (``ProviderModels``)."""

    def choose(_context: CallbackContext, request: LlmRequest) -> None:
        select = getattr(model, "select", None)
        if callable(select):
            select(request, name, thinking_level)

    return choose


def _check(
    config: HelperConfig,
    kits: Mapping[str, Kit[Any]],
    toolsets: Mapping[str, HelperToolset],
    models: set[str] | None,
) -> None:
    problems = []
    for spec in config.helpers:
        where = f"helper {spec.name}"
        if spec.kit_name not in kits:
            problems.append(f"{where}: no kit named {spec.kit_name}")
        given: set[str] = set()
        for ref in spec.toolsets:
            known = toolsets.get(ref.name)
            if known is None:
                problems.append(f"{where}: no toolset named {ref.name}")
                continue
            unknown = sorted(set(ref.tools) - known.tools) if ref.tools else []
            if unknown:
                problems.append(
                    f"{where}: {ref.name} has no tools {', '.join(unknown)}"
                )
            given |= known.tools if ref.tools is None else set(ref.tools)
        if spec.hand_in and spec.hand_in not in given:
            problems.append(f"{where}: it isn't given its hand-in tool {spec.hand_in}")
        if spec.model and models is not None and spec.model not in models:
            problems.append(
                f"{where}: model {spec.model} isn't one the assistant may use"
            )
    if problems:
        raise ValueError("Invalid helper configuration: " + "; ".join(problems))
