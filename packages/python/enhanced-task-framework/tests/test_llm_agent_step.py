"""LlmAgentStep behavior with a fake Gemini client — no network, no SDK required.

Covers: result persistence + run-context handoff, checkpoint reuse across the review
pause (the API is billed exactly once), transient-vs-permanent retry classification,
and prompt-template validation.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from etf import Actor, BatchStatus, Step, StepContext, StepResult, ValidationDecision
from etf.examples.llm_agent_step import LlmAgentStep, llm_retry_policy
from helpers import launch_request, make_harness


class FakeApiError(Exception):
    """Shape-compatible with google.genai.errors.APIError (has .code)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"api error {code}")
        self.code = code


def response(text: str | None):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=5, total_token_count=15
        ),
    )


class FakeGeminiClient:
    """Plays back a script of responses/exceptions and records every call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class CaptureDownstream(Step):
    """Downstream step proving the result is visible via the run context."""

    name = "downstream"

    def __init__(self) -> None:
        self.seen: str | None = None

    async def execute(self, ctx: StepContext) -> StepResult:
        self.seen = ctx.run_context.get("llm_result")
        return StepResult.completed()


def make_llm_harness(*script, downstream=None, retry_policy=None, **step_kwargs):
    client = FakeGeminiClient(*script)
    step = LlmAgentStep(client_factory=lambda: client, **step_kwargs)
    steps = [step] + ([downstream] if downstream else [])
    options = {"retry_policy": retry_policy} if retry_policy else {}
    launcher, operator, cfg, store = make_harness(*steps, definition_options=options)
    return client, step, launcher, operator, store


async def test_saves_response_and_hands_it_to_downstream_steps():
    downstream = CaptureDownstream()
    client, _, launcher, _, store = make_llm_harness(
        response("the answer"),
        downstream=downstream,
        prompt_template="Summarize: {ticket_text}",
    )
    run = await launcher.launch(launch_request(ticket_text="printer on fire"))

    assert run.status is BatchStatus.COMPLETED
    assert downstream.seen == "the answer"
    assert client.calls[0]["contents"] == "Summarize: printer on fire"

    persisted = (await store.find_step_runs(run.id))[0].execution_context
    assert persisted["response_text"] == "the answer"
    assert persisted["usage"]["total_tokens"] == 15


async def test_review_pause_reuses_checkpointed_response_no_double_billing():
    client, _, launcher, operator, store = make_llm_harness(
        response("draft output"),
        require_review=True,
        required_role="reviewer",
    )
    run = await launcher.launch(launch_request(prompt="write a draft"))
    assert run.status is BatchStatus.AWAITING_VALIDATION

    gate = await store.get_validation_request(run.open_validation_id)
    assert gate.payload["response_preview"] == "draft output"

    run = await operator.submit_validation_decision(
        gate.id, ValidationDecision.approve(Actor.human("u1", roles={"reviewer"}))
    )
    assert run.status is BatchStatus.COMPLETED
    assert len(client.calls) == 1  # re-execution consumed the checkpoint, not the API


async def test_rejected_review_stops_the_run_without_rebilling():
    client, _, launcher, operator, store = make_llm_harness(
        response("bad output"),
        require_review=True,
        retry_policy=llm_retry_policy(base_delay=timedelta(0)),
    )
    run = await launcher.launch(launch_request(prompt="p"))
    run = await operator.submit_validation_decision(
        run.open_validation_id, ValidationDecision.reject(Actor.human("u1"))
    )
    # REJECT halts at the operator level: STOPPED (restartable), step not re-executed.
    assert run.status is BatchStatus.STOPPED
    assert len(client.calls) == 1


async def test_transient_errors_are_retried_then_succeed():
    client, _, launcher, _, _ = make_llm_harness(
        FakeApiError(503),
        FakeApiError(429),  # rate limit counts as transient too
        response("third time lucky"),
        retry_policy=llm_retry_policy(base_delay=timedelta(0)),
    )
    run = await launcher.launch(launch_request(prompt="p"))
    assert run.status is BatchStatus.COMPLETED
    assert len(client.calls) == 3


async def test_permanent_errors_fail_immediately():
    client, _, launcher, _, store = make_llm_harness(
        FakeApiError(400),
        retry_policy=llm_retry_policy(base_delay=timedelta(0)),
    )
    run = await launcher.launch(launch_request(prompt="p"))
    assert run.status is BatchStatus.FAILED
    assert len(client.calls) == 1  # no retry on 4xx
    assert "PermanentLlmError" in run.failures[0].exception_type


async def test_empty_response_is_a_permanent_failure():
    client, _, launcher, _, _ = make_llm_harness(
        response(None),  # safety block / empty candidate
        retry_policy=llm_retry_policy(base_delay=timedelta(0)),
    )
    run = await launcher.launch(launch_request(prompt="p"))
    assert run.status is BatchStatus.FAILED
    assert len(client.calls) == 1


def tool_response(text: str, history):
    resp = response(text)
    resp.automatic_function_calling_history = history
    return resp


async def test_tools_are_passed_to_the_sdk_with_a_call_cap():
    async def lookup_order(order_id: str) -> dict:
        """Fetch an order."""
        return {"order_id": order_id, "status": "shipped"}

    client, _, launcher, _, _ = make_llm_harness(
        response("done"),
        tools=[lookup_order],
        max_tool_calls=3,
    )
    run = await launcher.launch(launch_request(prompt="p"))
    assert run.status is BatchStatus.COMPLETED
    config = client.calls[0]["config"]
    assert config["tools"] == [lookup_order]
    assert config["automatic_function_calling"] == {"maximum_remote_calls": 3}


async def test_no_tools_means_no_tool_config():
    client, _, launcher, _, _ = make_llm_harness(response("done"))
    await launcher.launch(launch_request(prompt="p"))
    config = client.calls[0]["config"]
    assert "tools" not in config and "automatic_function_calling" not in config


async def test_tool_call_history_is_checkpointed_and_shown_to_reviewers():
    history = [
        SimpleNamespace(parts=[SimpleNamespace(  # model asks for a tool
            function_call=SimpleNamespace(name="lookup_order", args={"order_id": "A-1"}),
            function_response=None,
        )]),
        SimpleNamespace(parts=[SimpleNamespace(  # SDK feeds the result back
            function_call=None,
            function_response=SimpleNamespace(name="lookup_order",
                                              response={"status": "shipped"}),
        )]),
    ]
    client, _, launcher, operator, store = make_llm_harness(
        tool_response("Order A-1 has shipped.", history),
        tools=[lambda: None],
        require_review=True,
    )
    run = await launcher.launch(launch_request(prompt="p"))
    assert run.status is BatchStatus.AWAITING_VALIDATION

    gate = await store.get_validation_request(run.open_validation_id)
    assert gate.payload["tool_calls"] == [
        {"type": "call", "tool": "lookup_order", "args": {"order_id": "A-1"}},
        {"type": "result", "tool": "lookup_order", "response": "{'status': 'shipped'}"},
    ]

    run = await operator.submit_validation_decision(
        gate.id, ValidationDecision.approve(Actor.human("u1"))
    )
    assert run.status is BatchStatus.COMPLETED
    assert len(client.calls) == 1  # tool loop not re-billed after the review pause
    persisted = (await store.find_step_runs(run.id))[0].execution_context
    assert persisted["tool_calls"][0]["tool"] == "lookup_order"


async def test_missing_template_parameter_fails_before_calling_the_api():
    client, _, launcher, _, _ = make_llm_harness(
        response("never used"),
        prompt_template="Summarize: {nonexistent_param}",
        retry_policy=llm_retry_policy(base_delay=timedelta(0)),
    )
    run = await launcher.launch(launch_request(prompt="p"))
    assert run.status is BatchStatus.FAILED
    assert client.calls == []  # failed fast, no API spend
