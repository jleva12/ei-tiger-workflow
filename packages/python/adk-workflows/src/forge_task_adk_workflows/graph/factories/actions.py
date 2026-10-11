"""
Actions: HTTP requests, transforms, filters and delays.

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
- ``filter``: goes through its list's items (None: none; one value: a list of
  it, as a loop does) and keeps those its condition is true for, read per item
  with the item under its item name and ``index`` beside the node's data. It
  hands on ``{count, results}``: the items kept, in order. Its condition is
  read in batches off the event loop, all of them within the evaluator's list
  timeout (``support.expressions``); past it, the run fails.
- ``delay``: waits, then hands on ``{resumed_at}``. A wait up to the inline
  threshold sleeps in place; a longer one pauses the run (``{kind: "delay",
  until}``) until the timer wakes it (``pauses.due_pauses``).
"""

import json
from collections.abc import Awaitable, Callable
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
from forge_task_adk_workflows.support.step_settings import (
    UNIT_SECONDS,
    DelayConfig,
    FilterConfig,
    HttpConfig,
    TransformConfig,
)


class CredentialRefused(StepFailed):
    """The step's credential can't sign its request (gone, not for where it
    goes, not signed in with): never retried."""


def http(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, HttpConfig)
    body_model = to_model(s.output_schema) if s.output_schema else None
    services = ctx.services
    evaluator = services.evaluator
    has_error_way = "error" in ctx.connected.get(node["id"], frozenset())

    async def sign(url: str, first: bool) -> tuple[dict[str, str], dict[str, str]]:
        # What the step's credential has a request to ``url`` send: the first
        # is refused where it may not go; a redirect elsewhere goes without it.
        if not s.credential:
            return {}, {}
        provider = services.credentials
        if provider is None:
            raise CredentialRefused("It uses one of the organization's credentials, which this worker can't use")
        try:
            auth = await provider(s.credential, organization_id=services.organization_id, url=url)
        except LookupError as error:
            if first:
                raise CredentialRefused(str(error)) from None
            return {}, {}
        return dict(auth.headers), dict(auth.params)

    async def request(data: dict[str, Any]) -> dict[str, Any]:
        url = (await evaluator.render(s.url, data, what="Its URL")).strip()
        headers = {
            header.name.strip(): await evaluator.render(header.value, data, what=f"Its {header.name} header")
            for header in s.headers
            if header.name.strip()
        }
        sent: dict[str, Any] = {}
        if s.method != "GET" and s.body.strip():
            body = await evaluator.evaluate(s.body, data, what="Its body")
            if isinstance(body, dict | list):
                sent["json"] = body
            elif body is not None:
                sent["content"] = body if isinstance(body, str) else json.dumps(body)
        last: StepFailed | None = None
        for attempt in range(1 + max(0, s.retries)):
            if attempt:
                await services.sleep(min(2**attempt, 30))
            try:
                response = await _send(services, s.method, url, headers, sent, seconds=s.timeout_seconds, sign=sign)
            except CredentialRefused:
                raise
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


#: What a request to a URL sends besides: headers and query parameters
#: (a credential's), and whether it's the first hop.
Sign = Callable[[str, bool], Awaitable[tuple[dict[str, str], dict[str, str]]]]


async def _unsigned(url: str, first: bool) -> tuple[dict[str, str], dict[str, str]]:
    return {}, {}


async def _send(
    services: RunServices,
    method: str,
    url: str,
    headers: dict[str, str],
    sent: dict[str, Any],
    *,
    seconds: float,
    sign: Sign = _unsigned,
) -> dict[str, Any]:
    # One request, its redirects followed one hop at a time, each checked.
    if services.http is not None:
        return await _exchange(services.http, services, method, url, headers, sent, seconds, sign)
    async with httpx.AsyncClient() as client:
        return await _exchange(client, services, method, url, headers, sent, seconds, sign)


async def _exchange(
    client: httpx.AsyncClient,
    services: RunServices,
    method: str,
    url: str,
    headers: dict[str, str],
    sent: dict[str, Any],
    seconds: float,
    sign: Sign = _unsigned,
) -> dict[str, Any]:
    for hop in range(MAX_REDIRECTS + 1):
        await check_url(
            url,
            allow_private=services.allow_private,
            allowed_hosts=list(services.allowed_hosts),
        )
        signed, params = await sign(url, hop == 0)
        try:
            async with client.stream(
                method,
                # Added to the URL's own query (httpx's params would replace it).
                httpx.URL(url).copy_merge_params(params) if params else url,
                headers={**headers, **signed},
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

    async def run(adk: Context, node_input: Any) -> Any:
        try:
            value = await evaluator.evaluate(s.expression, ctx.data(adk, node_input))
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


def filter_(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, FilterConfig)
    evaluator = ctx.services.evaluator
    item_name = s.item_name or "item"

    async def run(adk: Context, node_input: Any) -> dict[str, Any]:
        data = ctx.data(adk, node_input)
        try:
            found = await evaluator.evaluate(s.items, data, what="Its list")
            # JSONata gives one item on its own, not in a list.
            items = [] if found is None else found if isinstance(found, list) else [found]
            # In batches off the event loop, all of them within the evaluator's list timeout.
            kept = await evaluator.kept(s.condition, items, data, item_name=item_name)
        except StepFailed as error:
            raise failed(node, error) from None
        return {"count": len(kept), "results": kept}

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
