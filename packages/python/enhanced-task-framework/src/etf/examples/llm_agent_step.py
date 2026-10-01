"""``LlmAgentStep`` — a reusable ETF step that sends a task to Google Gemini.

Uses the Google Gen AI SDK (``pip install google-genai``; the import is lazy, so this
module loads fine without it — handy for tests that inject a fake client). The step is
written the way an LLM call *should* sit inside a durable job:

* **Async, non-blocking** — calls ``client.aio`` so the runner's heartbeat and control
  monitor keep running (never the sync client inside a step).
* **Checkpointed, not re-billed** — the response is written to the step context and
  checkpointed immediately. A crash after the checkpoint, a pause, or a restart
  re-enters the step and *reuses* the saved response instead of paying for a second
  (and different — LLMs are non-deterministic) generation.
* **Retry-classified** — SDK/network failures are wrapped into
  :class:`TransientLlmError` (429/5xx/timeouts — worth retrying) or
  :class:`PermanentLlmError` (4xx — retrying cannot help), so the job's
  :func:`llm_retry_policy` retries exactly the failures that can succeed.
* **Optionally human-gated** — ``require_review=True`` pauses the run with the
  generated text attached to a :class:`~etf.model.ValidationRequest`; an approver
  resumes (or rejects) it through the normal operator resume path.
* **Tool-using** — pass plain Python functions via ``tools=[...]`` and the SDK's
  automatic function calling lets Gemini invoke them (agent loop) before answering;
  the executed calls are checkpointed under ``tool_calls`` and surfaced to reviewers.

  ::

      async def lookup_order(order_id: str) -> dict:
          '''Fetch an order's status and total from the order service.'''
          return await orders_api.get(order_id)

      LlmAgentStep(tools=[lookup_order], max_tool_calls=5, ...)

The result is saved twice: durably in the step's execution context (auditable,
survives restarts) and into the *run* context under ``output_key`` so downstream
steps can read it: ``ctx.run_context.get("llm_result")``.

Quick demo (needs ``GEMINI_API_KEY``)::

    pip install -e . google-genai
    python -m etf.examples.llm_agent_step "Explain idempotency in two sentences."
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from etf import BackoffRetryPolicy, Step, StepContext, StepResult


class TransientLlmError(RuntimeError):
    """Gemini failure worth retrying: rate limit (429), 5xx, network timeout."""


class PermanentLlmError(RuntimeError):
    """Gemini failure a retry cannot fix: bad request, auth, blocked/empty output."""


def _fq(cls: type) -> str:
    """Fully-qualified name, as recorded in FailureRecord.exception_type."""
    return f"{cls.__module__}.{cls.__qualname__}"


def llm_retry_policy(
    max_attempts: int = 4,
    base_delay: timedelta = timedelta(seconds=2),
    max_delay: timedelta = timedelta(seconds=30),
) -> BackoffRetryPolicy:
    """Retry policy matched to the step's error classification: exponential backoff
    on :class:`TransientLlmError` only. Wire it into the job definition::

        JobDefinition(name="summarize", steps=[LlmAgentStep(...)],
                      retry_policy=llm_retry_policy())
    """
    return BackoffRetryPolicy(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        retryable_types=frozenset({_fq(TransientLlmError)}),
        non_retryable_types=frozenset({_fq(PermanentLlmError)}),
    )


def _classify(exc: BaseException) -> RuntimeError:
    """Map an SDK/network exception onto the transient/permanent split.

    Duck-types on the HTTP status (``google.genai.errors.APIError.code``) instead of
    importing SDK exception classes, so fakes and future SDK versions classify the
    same way. Anything without a status (connection reset, DNS, timeout) is transient.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        if code in (408, 429) or 500 <= code < 600:
            return TransientLlmError(f"Gemini transient error ({code}): {exc}")
        return PermanentLlmError(f"Gemini rejected the request ({code}): {exc}")
    return TransientLlmError(f"Gemini call failed: {exc}")


def _summarize_tool_calls(history: Any) -> list[dict[str, Any]]:
    """Compact, JSON-friendly record of the agent's tool loop for the checkpoint.

    Walks the SDK's ``automatic_function_calling_history`` (a list of Content turns)
    defensively via getattr, so fakes and SDK model changes degrade to "no record"
    rather than a crash.
    """
    events: list[dict[str, Any]] = []
    for content in history or []:
        for part in getattr(content, "parts", None) or []:
            call = getattr(part, "function_call", None)
            if call is not None and getattr(call, "name", None):
                events.append({
                    "type": "call",
                    "tool": call.name,
                    "args": dict(getattr(call, "args", None) or {}),
                })
            result = getattr(part, "function_response", None)
            if result is not None and getattr(result, "name", None):
                events.append({
                    "type": "result",
                    "tool": result.name,
                    "response": str(getattr(result, "response", ""))[:500],
                })
    return events


class LlmAgentStep(Step):
    """Send one prompt to Gemini and durably save the response.

    ``prompt_template`` is rendered with ``str.format`` against the run's merged
    parameters — e.g. ``"Summarize this ticket: {ticket_text}"`` with a launch
    parameter ``ticket_text``. The default template just forwards a ``prompt``
    parameter.

    Construction knobs:

    :param name: step name, unique within the job (several LLM steps can coexist).
    :param model: Gemini model id.
    :param output_key: run-context key downstream steps read the text from.
    :param tools: Python functions the agent may call. Passed to the SDK's
        **automatic function calling**: Gemini sees each function's name, type hints
        and docstring, decides when to call it, the SDK executes it and feeds the
        result back, looping until the model produces a final text answer. Tool
        rules mirror step rules — tools run on the shared event loop, so prefer
        ``async def`` tools (the ``aio`` client awaits them) and never block; and
        tools with side effects must be **idempotent**, because a retry or restart
        that re-enters this step before the checkpoint re-runs the whole tool loop.
        The executed calls are recorded in the step context under ``tool_calls``
        (and shown to reviewers when ``require_review`` is on).
    :param max_tool_calls: cap on tool invocations per generation — the loop
        terminator for an agent that keeps calling tools.
    :param require_review: pause for human approval of the generated text; pair with
        ``required_role`` to restrict who may approve.
    :param api_key: explicit key; by default the SDK reads ``GEMINI_API_KEY`` /
        ``GOOGLE_API_KEY`` from the environment.
    :param client_factory: zero-arg callable returning a client (dependency
        injection for tests, or to share one configured client between steps).
    """

    def __init__(
        self,
        *,
        name: str = "llm_agent",
        model: str = "gemini-2.5-flash",
        prompt_template: str = "{prompt}",
        system_instruction: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        output_key: str = "llm_result",
        tools: Sequence[Callable[..., Any]] | None = None,
        max_tool_calls: int = 10,
        require_review: bool = False,
        required_role: str | None = None,
        review_sla_seconds: int | None = None,
        api_key: str | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.prompt_template = prompt_template
        self.system_instruction = system_instruction
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.output_key = output_key
        self.tools = list(tools or [])
        self.max_tool_calls = max_tool_calls
        self.require_review = require_review
        self.required_role = required_role
        self.review_sla_seconds = review_sla_seconds
        self._api_key = api_key
        self._client_factory = client_factory
        self._client: Any = None

    # One client per step instance per process; all calls run on the worker's single
    # event loop (the AsyncBridge loop in a synchronous host), which is what httpx expects.
    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                # Lazy import keeps this optional example importable without the SDK.
                genai: Any = importlib.import_module("google.genai")

                self._client = genai.Client(api_key=self._api_key) if self._api_key else genai.Client()
        return self._client

    def _render_prompt(self, ctx: StepContext) -> str:
        try:
            return self.prompt_template.format(**ctx.params.merged())
        except KeyError as exc:
            raise PermanentLlmError(
                f"prompt template references missing job parameter {exc}"
            ) from exc

    async def execute(self, ctx: StepContext) -> StepResult:
        # Resume path: a prior attempt already generated and checkpointed a response
        # (e.g. we paused for review, or a later step crashed). Never re-bill the API
        # for a text we already have.
        if ctx.get("response_text") is None:
            prompt = self._render_prompt(ctx)
            config: dict[str, Any] = {
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            }
            if self.system_instruction:
                config["system_instruction"] = self.system_instruction
            if self.tools:
                # The SDK's automatic function calling: introspects the callables,
                # executes them as the model asks, loops until a final text answer.
                config["tools"] = list(self.tools)
                config["automatic_function_calling"] = {
                    "maximum_remote_calls": self.max_tool_calls,
                }
            try:
                response = await self._get_client().aio.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
            except Exception as exc:
                raise _classify(exc) from exc

            text = getattr(response, "text", None)
            if not text:
                raise PermanentLlmError(
                    "Gemini returned no text (safety block or empty candidate); "
                    "inspect the prompt — retrying the same request will not help"
                )
            ctx.put("response_text", text)
            ctx.put("model", self.model)
            tool_calls = _summarize_tool_calls(
                getattr(response, "automatic_function_calling_history", None)
            )
            if tool_calls:
                ctx.put("tool_calls", tool_calls)  # durable audit of the agent's loop
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                ctx.put("usage", {
                    "prompt_tokens": getattr(usage, "prompt_token_count", None),
                    "output_tokens": getattr(usage, "candidates_token_count", None),
                    "total_tokens": getattr(usage, "total_token_count", None),
                })
            await ctx.checkpoint()  # durable BEFORE the gate / downstream steps

        if self.require_review:
            outcome = await ctx.request_validation(
                reason=f"review LLM output of step {self.name!r}",
                payload={
                    "model": ctx.get("model"),
                    "response_preview": str(ctx.get("response_text"))[:2000],
                    "usage": ctx.get("usage"),
                    # Reviewers see WHAT the agent did, not just what it said.
                    "tool_calls": ctx.get("tool_calls"),
                },
                required_role=self.required_role,
                sla_seconds=self.review_sla_seconds,
            )
            if not outcome.approved:
                raise PermanentLlmError("LLM output rejected by reviewer")

        # Hand the text to downstream steps via the shared run context.
        ctx.run_context.put(self.output_key, ctx.get("response_text"))
        return StepResult(
            write_count=1,
            attributes={
                "usage": ctx.get("usage"),
                "tool_call_count": sum(
                    1 for e in (ctx.get("tool_calls") or []) if e.get("type") == "call"
                ),
            },
        )


# --------------------------------------------------------------------------- #
# Runnable demo: python -m etf.examples.llm_agent_step "your prompt"
# --------------------------------------------------------------------------- #
def _main() -> None:  # pragma: no cover - manual demo
    import asyncio
    import sys
    import uuid

    from etf import (
        EtfConfig, JobDefinition, JobLauncher, JobParameters, JobRegistry, LaunchRequest,
    )
    from etf.audit import StoreBackedAuditSink
    from etf.locking import InMemoryLockProvider
    from etf.stores.memory import InMemoryStateStore

    prompt = " ".join(sys.argv[1:]) or "Explain idempotency in two sentences."

    async def run() -> None:
        store = InMemoryStateStore()
        await store.initialize()
        registry = JobRegistry()
        registry.register(JobDefinition(
            name="llm_task", steps=[LlmAgentStep()], retry_policy=llm_retry_policy(),
        ))
        cfg = EtfConfig(store=store, audit=StoreBackedAuditSink(store),
                        lock_provider=InMemoryLockProvider(), registry=registry)
        run = await JobLauncher(cfg).launch(LaunchRequest(
            job_name="llm_task",
            parameters=JobParameters(identifying={"request_id": uuid.uuid4().hex},
                                     non_identifying={"prompt": prompt}),
        ))
        print(f"run {run.id}: {run.status.value}")
        step_run = (await store.find_step_runs(run.id))[0]
        context = step_run.execution_context
        print(context.get("response_text") if isinstance(context, dict) else None)

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    _main()
