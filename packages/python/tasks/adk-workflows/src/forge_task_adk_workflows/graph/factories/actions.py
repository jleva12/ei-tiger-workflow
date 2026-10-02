"""
Actions: HTTP requests, transforms and delays.

- ``http``: calls a URL and hands on ``{status, headers, body}`` by its
  Success way. Its URL and headers are text with ``{{ }}`` in them; its body a
  JSONata expression (an object or list is sent as JSON). Private addresses
  are refused, redirects included. 408, 425, 429, 5xx and no answer are
  retried (2s, 4s… apart); anything else but a 2xx, or no answer after its
  retries, takes its Error way, handing on nothing, with its error as
  ``steps.<id>.error``. A 2xx whose body doesn't fit its output schema takes
  it too; a body that fits is handed on as its schema has it. With no Error
  way, the run fails.
- ``transform``: hands on what its JSONata expression makes, held to its
  output schema when it has one (coerced; the run fails when it doesn't fit).
- ``delay``: waits, then hands on ``{resumed_at}``. A wait up to the inline
  threshold sleeps in place; a longer one pauses the run (``{kind: "delay",
  until}``) until the timer wakes it (``pauses.due_pauses``).
"""

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import httpx
from google.adk import Context, Event
from google.adk.events import RequestInput
from google.adk.workflow import FunctionNode
from pydantic import ValidationError

from forge_task_adk_workflows.graph.data import ERROR_KEY
from forge_task_adk_workflows.graph.errors import RunFailed
from forge_task_adk_workflows.graph.factories.base import (
    BuildContext,
    asked,
    failed,
    interrupt_id,
    label_of,
    settings_of,
    text,
)
from forge_task_adk_workflows.graph.names import adk_name
from forge_task_adk_workflows.graph.schemas import held_to, problems, to_model
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.http import MAX_REDIRECTS, RETRYABLE, check_url, parse_body, read_body
from forge_task_adk_workflows.support.step_settings import UNIT_SECONDS, DelayConfig, HttpConfig, TransformConfig


def http(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, HttpConfig)
    body_model = to_model(s.output_schema) if s.output_schema else None
    services = ctx.services
    evaluator = services.evaluator
    has_error_way = "error" in ctx.connected.get(node["id"], frozenset())

    async def request(data: dict[str, Any]) -> dict[str, Any]:
        url = evaluator.render(s.url, data, what="Its URL").strip()
        headers = {
            header.name.strip(): evaluator.render(header.value, data, what=f"Its {header.name} header")
            for header in s.headers
            if header.name.strip()
        }
        sent: dict[str, Any] = {}
        if s.method != "GET" and s.body.strip():
            body = evaluator.evaluate(s.body, data, what="Its body")
            if isinstance(body, dict | list):
                sent["json"] = body
            elif body is not None:
                sent["content"] = body if isinstance(body, str) else json.dumps(body)
        last: StepFailed | None = None
        for attempt in range(1 + max(0, s.retries)):
            if attempt:
                await services.sleep(min(2**attempt, 30))
            try:
                response = await _send(services, s.method, url, headers, sent, seconds=s.timeout_seconds)
            except StepFailed as error:
                last = error
                if error.status is None or error.status in RETRYABLE:
                    continue
                raise
            if 200 <= response["status"] < 300:
                if body_model is not None:
                    try:
                        response["body"] = held_to(body_model, response["body"])
                    except ValidationError as error:
                        # Not retried: the same answer wouldn't fit either.
                        raise StepFailed(
                            "Its response doesn't fit its declared output: " + problems(error, "body"),
                            status=response["status"],
                            details={"body": response["body"]},
                        ) from None
                return response
            last = StepFailed(
                f"It answered {response['status']}",
                status=response["status"],
                details={"body": response["body"]},
            )
            if response["status"] not in RETRYABLE:
                break
        assert last is not None
        raise last

    async def run(adk: Context, node_input: Any) -> Event:
        try:
            response = await request(ctx.data(adk, node_input))
        except StepFailed as error:
            if not has_error_way:
                raise failed(node, error) from None
            return Event(route="error", custom_metadata={ERROR_KEY: error.as_error()})  # type: ignore[call-arg]  # ADK's shorthand for actions.route
        return Event(output=response, route="success")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run)


async def _send(
    services: RunServices,
    method: str,
    url: str,
    headers: dict[str, str],
    sent: dict[str, Any],
    *,
    seconds: float,
) -> dict[str, Any]:
    # One request, its redirects followed one hop at a time, each checked.
    if services.http is not None:
        return await _exchange(services.http, services, method, url, headers, sent, seconds)
    async with httpx.AsyncClient() as client:
        return await _exchange(client, services, method, url, headers, sent, seconds)


async def _exchange(
    client: httpx.AsyncClient,
    services: RunServices,
    method: str,
    url: str,
    headers: dict[str, str],
    sent: dict[str, Any],
    seconds: float,
) -> dict[str, Any]:
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(
            url,
            allow_private=services.allow_private,
            allowed_hosts=list(services.allowed_hosts),
        )
        try:
            async with client.stream(
                method,
                url,
                headers=headers,
                timeout=seconds,
                follow_redirects=False,
                **sent,
            ) as response:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    if response.status_code in (301, 302, 303):
                        method, sent = "GET", {}
                    continue
                raw = await read_body(response, services.max_response_bytes)
                return {
                    "status": response.status_code,
                    "headers": {k.lower(): v for k, v in response.headers.items()},
                    "body": parse_body(raw, response.headers.get("content-type", "")),
                }
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as error:
            raise StepFailed(f"{url} didn't answer: {type(error).__name__}: {error}") from error
        except TimeoutError as error:
            raise StepFailed(f"{url} didn't answer in {seconds:g} seconds") from error
    raise StepFailed(f"{url} redirected more than {MAX_REDIRECTS} times")


def transform(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, TransformConfig)
    model = to_model(s.output_schema) if s.output_schema else None
    evaluator = ctx.services.evaluator

    def run(adk: Context, node_input: Any) -> Any:
        try:
            value = evaluator.evaluate(s.expression, ctx.data(adk, node_input))
        except StepFailed as error:
            raise failed(node, error) from None
        if model is not None:
            try:
                value = held_to(model, value)
            except ValidationError as error:
                raise RunFailed(
                    f"{label_of(node)} failed: What it made doesn't fit its declared "
                    f"output: {problems(error, 'output')}",
                    step=node["id"],
                ) from None
        return value

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run)


def delay(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, DelayConfig)
    seconds = max(0.0, s.amount * UNIT_SECONDS[s.unit])
    services = ctx.services
    what = f"a delay of {s.amount:g} {s.unit}"

    async def run(adk: Context, node_input: Any) -> Any:
        if seconds <= services.inline_delay_seconds:
            await services.sleep(seconds)
            return {"resumed_at": services.clock().isoformat()}
        # A long one: asked once, again if woken early, each time under an
        # ID of its own.
        first = key = interrupt_id(node, adk)
        times = 1
        while key in adk.resume_inputs:
            times += 1
            key = f"{first}#{times}"
        if key == first:
            until = services.clock() + timedelta(seconds=seconds)
        else:
            until = datetime.fromisoformat(asked(adk, first).get("until") or services.clock().isoformat())
        remaining = (until - services.clock()).total_seconds()
        if remaining > services.inline_delay_seconds:
            return RequestInput(
                interrupt_id=key,
                message=f"Waiting until {until.isoformat(timespec='seconds')}: {what}",
                payload={
                    "kind": "delay",
                    "step": node["id"],
                    "until": until.isoformat(),
                },
                response_schema=None,
            )
        if remaining > 0:
            await services.sleep(remaining)
        return {"resumed_at": services.clock().isoformat()}

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run, rerun_on_resume=True)
