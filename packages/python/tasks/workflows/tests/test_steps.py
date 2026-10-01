"""The steps that reach outside the run: HTTP, other workflows, agents."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from forge_task_workflows.errors import StepFailed, WorkflowFailed
from forge_task_workflows.services.http_guard import check_url
from forge_tasks.control import LocalJobControl, RunState, WaitingUntil

from .helpers import ORGANIZATION, Clock, FakeAdmin, FakeGemini, run, services, workflow

START = ("start", "entry", {})


async def test_an_http_step_sends_its_body_and_hands_on_the_response() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 9}, headers={"X-Trace": "abc"})

    doc = workflow(
        [
            START,
            (
                "post",
                "http",
                {
                    "method": "POST",
                    "url": "https://api.example.com/issues/{{ input.key }}",
                    "headers": [{"id": "h", "name": "Authorization", "value": "Token {{ input.token }}"}],
                    "body": '{"text": "Fix " & input.key}',
                },
            ),
            ("done", "end", {"outcome": "succeeded", "result": "steps.post.output"}),
        ],
        [("start", "next", "post"), ("post", "success", "done")],
    )
    result = await run(doc, input={"key": "XMEN-12", "token": "t0k"}, svc=services(http=api))
    assert str(seen[0].url) == "https://api.example.com/issues/XMEN-12"
    assert seen[0].headers["authorization"] == "Token t0k"
    assert json.loads(seen[0].content) == {"text": "Fix XMEN-12"}
    response = result.detail["result"]
    assert (response["status"], response["body"], response["headers"]["x-trace"]) == (201, {"id": 9}, "abc")


async def test_an_http_step_retries_then_takes_its_error_way() -> None:
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="busy")

    clock = Clock()
    doc = workflow(
        [
            START,
            ("get", "http", {"method": "GET", "url": "https://api.example.com/x", "retries": 2}),
            ("failed", "end", {"outcome": "succeeded", "result": "steps.get.error"}),
        ],
        [("start", "next", "get"), ("get", "error", "failed")],
    )
    result = await run(doc, svc=services(http=api, clock=clock))
    assert len(calls) == 3
    assert clock.slept == [2, 4]
    assert result.detail["result"] == {"message": "It answered 503", "status": 503, "body": "busy"}


async def test_an_http_response_that_doesnt_fit_its_declared_output_takes_the_error_way() -> None:
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "not a number"})

    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    doc = workflow(
        [
            START,
            (
                "get",
                "http",
                {"method": "GET", "url": "https://api.example.com/x", "retries": 2, "output_schema": schema},
            ),
            ("ok", "end", {"outcome": "succeeded", "result": "steps.get.output.body"}),
            ("failed", "end", {"outcome": "succeeded", "result": "steps.get.error"}),
        ],
        [("start", "next", "get"), ("get", "success", "ok"), ("get", "error", "failed")],
    )
    result = await run(doc, svc=services(http=api))
    # It isn't retried: the same answer wouldn't fit either.
    assert len(calls) == 1
    error = result.detail["result"]
    assert error["status"] == 200
    assert error["message"].startswith("Its response doesn't fit its declared output: id: ")
    assert error["body"] == {"id": "not a number"}


async def test_http_steps_never_reach_a_private_network() -> None:
    for url in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5",
        "http://[::1]/",
    ):
        with pytest.raises(StepFailed, match="private network"):
            await check_url(url, allow_private=False, allowed_hosts=[])
    with pytest.raises(StepFailed, match="must start with http"):
        await check_url("file:///etc/passwd", allow_private=False, allowed_hosts=[])
    assert await check_url("http://127.0.0.1/x", allow_private=True, allowed_hosts=[]) == "http://127.0.0.1/x"


async def test_run_workflow_starts_the_other_and_waits_for_its_result() -> None:
    admin = FakeAdmin()
    doc = workflow(
        [
            START,
            ("child", "subworkflow", {"workflow": "wf_other", "wait": True}),
            ("done", "end", {"outcome": "succeeded", "result": "steps.child.output"}),
        ],
        [("start", "next", "child"), ("child", "next", "done")],
    )
    control = LocalJobControl(instance="parent-run")
    svc = services(admin=admin)
    with pytest.raises(WaitingUntil):
        await run(doc, input={"n": 1}, control=control, svc=svc)
    assert admin.started == [
        {
            "organization_id": ORGANIZATION,
            "user_id": "member-1",
            "input": {"n": 1},
            "parent": "parent-run/child#1",
            "depth": 1,
        }
    ]
    control.runs[frozenset({"parent": "parent-run/child#1"}.items())] = RunState(
        id="t2",
        status="succeeded",
        result={"status": "ok", "detail": {"outcome": "succeeded", "result": {"doubled": 2}}},
    )
    result = await run(doc, input={"n": 1}, control=control, svc=svc)
    assert result.detail["result"] == {"doubled": 2}
    assert len(admin.started) == 1


async def test_an_agent_answers_in_json_held_to_its_schema() -> None:
    gemini = FakeGemini(["It's a bug, and it's severe.", '{"kind": "bug", "severity": "high"}'])
    doc = workflow(
        [
            START,
            (
                "classify",
                "agent",
                {
                    "instructions": "Classify {{ input.key }}.",
                    "output": "json",
                    "output_schema": json.dumps(
                        {"type": "object", "required": ["kind"], "properties": {"kind": {"enum": ["bug", "question"]}}}
                    ),
                },
            ),
            ("done", "end", {"outcome": "succeeded", "result": "steps.classify.output"}),
        ],
        [("start", "next", "classify"), ("classify", "next", "done")],
    )
    result = await run(doc, input={"key": "XMEN-12"}, svc=services(gemini=gemini))
    assert result.detail["result"] == {"kind": "bug", "severity": "high"}
    first = gemini.requests[0]
    assert first["model"] == "gemini-test"
    assert first["config"].system_instruction == "Classify XMEN-12."
    # It's offered no tools, then asked for its answer as JSON.
    assert first["config"].tools is None
    assert gemini.requests[1]["config"].response_json_schema["required"] == ["kind"]


def two_providers() -> Any:
    """A model provider configuration with Gemini (the default) and an OpenAI-compatible provider."""
    from forge_common.model_provider import ModelProviderConfig

    return ModelProviderConfig.model_validate(
        {
            "version": 1,
            "default": {"provider": "google", "model": "gemini-test"},
            "providers": {
                "google": {
                    "name": "Google Gemini",
                    "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
                    "api": "google-generative-ai",
                    "auth": {"type": "apiKey", "key": "k1"},
                    "models": [{"id": "gemini-test", "reasoning": True, "input": ["text"]}],
                },
                "acme": {
                    "name": "Acme",
                    "baseUrl": "https://acme.test/v1",
                    "api": "openai-responses",
                    "auth": {"type": "apiKey", "key": "k2"},
                    "models": [
                        {
                            "id": "acme-large",
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": 100000,
                            "maxTokens": 4096,
                        }
                    ],
                },
            },
        }
    )


async def test_an_agent_runs_on_the_model_and_thinking_level_it_names() -> None:
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}}
    gemini = FakeGemini(["It's 42.", '```json\n{"answer": 42}\n```'], config=two_providers())
    doc = workflow(
        [
            START,
            (
                "ask",
                "agent",
                {
                    "instructions": "Answer.",
                    "model": {"provider": "acme", "name": "acme-large"},
                    "thinking_level": "high",
                    "output": "json",
                    "output_schema": json.dumps(schema),
                },
            ),
            ("done", "end", {"outcome": "succeeded", "result": "steps.ask.output.answer"}),
        ],
        [("start", "next", "ask"), ("ask", "next", "done")],
    )
    result = await run(doc, svc=services(gemini=gemini))
    assert result.detail["result"] == 42
    # Both calls ran on the model it named, thinking as it said.
    assert [(call.model.ref, call.thinking_level) for call in gemini.calls] == [("acme/acme-large", "high")] * 2
    # A model that isn't Gemini is asked for JSON its own API's way.
    final = gemini.requests[-1]["config"]
    assert final.response_schema == schema and final.response_json_schema is None


async def test_an_agent_without_a_model_runs_on_the_default() -> None:
    gemini = FakeGemini(["Hello."], config=two_providers())
    doc = workflow(
        [START, ("ask", "agent", {"instructions": "Say hello."}), ("done", "end", {"result": "steps.ask.output"})],
        [("start", "next", "ask"), ("ask", "next", "done")],
    )
    result = await run(doc, svc=services(gemini=gemini))
    assert result.detail["result"] == "Hello."
    assert [(call.model.ref, call.thinking_level) for call in gemini.calls] == [("google/gemini-test", None)]


async def test_an_agent_naming_a_model_forge_doesnt_offer_fails_saying_which_it_does() -> None:
    gemini = FakeGemini([], config=two_providers())
    doc = workflow(
        [START, ("ask", "agent", {"instructions": "Answer.", "model": {"provider": "openai", "name": "gpt-9"}})],
        [("start", "next", "ask")],
    )
    with pytest.raises(
        WorkflowFailed,
        match="openai/gpt-9 isn't one Forge offers here; pick one of: google/gemini-test, acme/acme-large",
    ):
        await run(doc, svc=services(gemini=gemini))
    assert gemini.calls == []


async def test_an_agent_without_a_key_says_it_isnt_set_up() -> None:
    doc = workflow([START, ("a", "agent", {"instructions": "Hi"})], [("start", "next", "a")])
    with pytest.raises(WorkflowFailed, match="Agent steps aren't set up here"):
        await run(doc)
