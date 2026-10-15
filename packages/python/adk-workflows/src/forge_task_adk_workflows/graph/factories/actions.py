"""
Actions: HTTP requests, Bash commands, transforms, filters and delays.

- ``http``: calls a URL and hands on ``{status, headers, body}`` by its
  Success way. Its URL, query parameters and headers are text with ``{{ }}``
  in them; its body a JSONata expression (an object or list is sent as JSON).
  Its query parameters are added to the URL's own, each name and value
  percent-encoded, so they arrive as they were filled in: a value that's one
  ``{{ }}`` and comes to nothing leaves its parameter out, and a list sends it
  once per item. What the URL can't hold as it is (a space) is encoded too. Private addresses
  are refused, redirects included. 408, 425, 429, 5xx and no answer are
  retried (2s, 4s… apart); anything else but a 2xx, or no answer after its
  retries, takes its Error way, handing on nothing, with its error as
  ``steps.<id>.error``. A 2xx whose body doesn't fit its output schema takes
  it too; a body that fits is handed on as its schema has it. With no Error
  way, the run fails.
- ``bash``: runs its command by bash on the worker's machine, starting in its
  folder of the run's folder (``support.commands``), or in a sandbox of its
  own, the run's folder copied in and its changes back
  (``support.sandbox_commands``), and hands on ``{exit_code, stdout, stderr,
  cwd, truncated}`` by its Success way when it exits with 0.
  Its command runs as it's written; its environment variables (templates) and
  ``WORKSPACE`` (the run's folder) are how it reads the run's data. Any other
  exit code, a folder outside the run's folder or a command that runs past
  its timeout takes its Error way, handing on nothing, with its error
  (``{message, exit_code?, stdout, stderr}``) as ``steps.<id>.error``. With no
  Error way, the run fails; and so does a run on a worker that doesn't run
  commands, or without a folder (its start doesn't allow a file system). A
  run tried again runs its command again: write commands that can.
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

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urljoin

import httpx
from forge_common.urls import with_query
from google.adk import Context, Event
from google.adk.events import RequestInput
from google.adk.workflow import FunctionNode
from pydantic import ValidationError

from forge_task_adk_workflows.graph.data import ERROR_KEY
from forge_task_adk_workflows.graph.errors import AgentBuildError, RunFailed
from forge_task_adk_workflows.graph.factories.base import (
    BuildContext,
    asked,
    called_with,
    failed,
    interrupt_id,
    label_of,
    settings_of,
    text,
    where_of,
)
from forge_task_adk_workflows.graph.names import adk_name
from forge_task_adk_workflows.graph.schemas import held_to, problems, to_model
from forge_task_adk_workflows.graph.services import RunServices
from forge_task_adk_workflows.support.commands import (
    NO_COMMANDS,
    UNASKED,
    VARIABLE,
    WORKSPACE_VARIABLE,
    Finished,
    folder_in,
    last_line,
    run_command,
)
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.expressions import TEMPLATE, as_text
from forge_task_adk_workflows.support.http import (
    MAX_REDIRECTS,
    RETRYABLE,
    check_url,
    parse_body,
    read_body,
    statuses_of,
)
from forge_task_adk_workflows.support.step_settings import (
    UNIT_SECONDS,
    BashConfig,
    DelayConfig,
    FilterConfig,
    HttpConfig,
    TransformConfig,
)


class CredentialRefused(StepFailed):
    """The step's credential can't sign its request (gone, not for where it
    goes, not signed in with): never retried."""


#: What a request to a URL sends besides: headers and query parameters
#: (a credential's), and whether it's the first hop.
Sign = Callable[[str, bool], Awaitable[tuple[dict[str, str], dict[str, str]]]]


def signer(services: RunServices, credential: str) -> Sign:
    """
    :param credential: One of the organization's credentials, by ID; empty for none.
    :return: What it has a request to a URL send: the first request is
        refused (:class:`CredentialRefused`) where it may not go; a redirect
        elsewhere goes without it.
    """

    async def sign(url: str, first: bool) -> tuple[dict[str, str], dict[str, str]]:
        if not credential:
            return {}, {}
        provider = services.credentials
        if provider is None:
            raise CredentialRefused("It uses one of the organization's credentials, which this worker can't use")
        try:
            auth = await provider(credential, organization_id=services.organization_id, url=url)
        except LookupError as error:
            if first:
                raise CredentialRefused(str(error)) from None
            return {}, {}
        return dict(auth.headers), dict(auth.params)

    return sign


def http(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, HttpConfig)
    body_model = to_model(s.output_schema) if s.output_schema else None
    services = ctx.services
    evaluator = services.evaluator
    has_error_way = "error" in ctx.connected.get(node["id"], frozenset())
    sign = signer(services, s.credential)
    ways = _status_ways(node, s)

    def way_for(status: int) -> str | None:
        # The first status way whose codes hold it.
        return next((way for way, holds in ways if holds(status)), None)

    async def query(data: dict[str, Any]) -> list[tuple[str, str]]:
        # Its query parameters, filled in: one whose value is one {{ }} that
        # comes to nothing is left out; a list sends it once per item.
        pairs: list[tuple[str, str]] = []
        for row in s.query:
            name = (await evaluator.render(row.name, data, what="A query parameter's name")).strip()
            if not name:
                continue
            what = f"Its {name} query parameter"
            whole = _whole(row.value)
            if whole is None:
                pairs.append((name, await evaluator.render(row.value, data, what=what)))
                continue
            value = await evaluator.evaluate(whole, data, what=what)
            pairs.extend(
                (name, as_text(item)) for item in (value if isinstance(value, list) else [value]) if item is not None
            )
        return pairs

    async def prepared(data: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, Any]]:
        # Its URL, headers and body, filled in.
        url = with_query((await evaluator.render(s.url, data, what="Its URL")).strip(), await query(data))
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
        return url, headers, sent

    async def request(url: str, headers: dict[str, str], sent: dict[str, Any]) -> tuple[dict[str, Any], str]:
        # Its response, and the way it takes: a status way's, else Success.
        last: StepFailed | None = None
        for attempt in range(1 + max(0, s.retries)):
            if attempt:
                await services.sleep(min(2**attempt, 30))
            try:
                response = await send(services, s.method, url, headers, sent, seconds=s.timeout_seconds, sign=sign)
            except CredentialRefused:
                raise
            except StepFailed as error:
                last = error
                if error.status is None or error.status in RETRYABLE:
                    continue
                raise
            way = way_for(response["status"])
            if way is not None:
                # A status with a way of its own is an answer: not retried, nor
                # held to what a success returns.
                return response, way
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
                return response, "success"
            last = StepFailed(
                answered(s.method, url, response["status"], response["body"]),
                status=response["status"],
                details={"body": response["body"], "method": s.method, "url": url},
            )
            if response["status"] not in RETRYABLE:
                break
        assert last is not None
        raise last

    async def run(adk: Context, node_input: Any) -> AsyncGenerator[Event]:
        try:
            url, headers, sent = await prepared(ctx.data(adk, node_input))
            # The request, kept before it's sent (its headers by name: their
            # values may sign it): the run page shows it, though it fail.
            shown: dict[str, Any] = {"method": s.method, "url": url}
            if headers:
                shown["headers"] = sorted(headers)
            if sent:
                shown["body"] = sent.get("json", sent.get("content"))
            yield called_with(shown)
            response, way = await request(url, headers, sent)
        except StepFailed as error:
            if not has_error_way:
                raise failed(node, error) from None
            yield Event(route="error", custom_metadata={ERROR_KEY: error.as_error()})  # type: ignore[call-arg]  # ADK's shorthand for actions.route
            return
        yield Event(output=response, route=way)  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run)


def _status_ways(node: dict[str, Any], s: HttpConfig) -> list[tuple[str, Callable[[int], bool]]]:
    """
    :return: An HTTP step's status ways, in order: each one's ID and whether
        a status is one of its codes.
    :raises AgentBuildError: When one names no status, or names what isn't one.
    """
    ways: list[tuple[str, Callable[[int], bool]]] = []
    for index, way in enumerate(s.statuses, 1):
        if not way.codes.strip():
            raise AgentBuildError(f"{where_of(node)}: give its status way {index} the statuses that take it.")
        try:
            ways.append((way.id, statuses_of(way.codes)))
        except ValueError as error:
            raise AgentBuildError(f"{where_of(node)}: its status way {way.codes.strip()!r}: {error}.") from None
    return ways


#: The most of a response's body an HTTP failure's message quotes.
SAID_CHARS = 300
#: The fields an API's error body says what went wrong in, most telling first.
_SAYING = ("message", "error_description", "detail", "error", "title", "errors")


def answered(method: str, url: str, status: int, body: Any) -> str:
    """
    How an HTTP failure reads: the request, the status (and its phrase), and
    what the API said, cut short: ``GET https://x/y answered 404 Not Found:
    No such customer``. The whole body is the error's ``body``.
    """
    try:
        phrase = f" {HTTPStatus(status).phrase}"
    except ValueError:
        phrase = ""
    said = _said(body)
    return f"{method} {url} answered {status}{phrase}" + (f": {said}" if said else "")


def _said(body: Any) -> str:
    # What an error's body says: its message field (APIs' usual ones), else
    # the body itself, as text, on one line.
    if isinstance(body, dict):
        for key in _SAYING:
            found = body.get(key)
            if found not in (None, "", [], {}):
                body = found
                break
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        body = body["message"]
    said = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str) if body else ""
    said = " ".join(said.split())
    return said if len(said) <= SAID_CHARS else f"{said[:SAID_CHARS]}…"


def _whole(template: str) -> str | None:
    """:return: The expression a template is when it's one ``{{ }}`` and nothing else; None otherwise."""
    stripped = template.strip()
    found = TEMPLATE.findall(stripped)
    return found[0] if len(found) == 1 and TEMPLATE.fullmatch(stripped) else None


async def _unsigned(url: str, first: bool) -> tuple[dict[str, str], dict[str, str]]:
    return {}, {}


async def send(
    services: RunServices,
    method: str,
    url: str,
    headers: dict[str, str],
    sent: dict[str, Any],
    *,
    seconds: float,
    sign: Sign = _unsigned,
) -> dict[str, Any]:
    """
    One request, its redirects followed one hop at a time, each checked
    (``support.http.check_url``) and signed (``sign``): HTTP steps', and the
    GitHub steps' calls to GitHub's API.

    :return: Its ``{status, headers, body}``, whatever the status.
    :raises StepFailed: When it gets no answer, or is redirected too often.
    """
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


def bash(node: dict[str, Any], ctx: BuildContext) -> FunctionNode:
    s = settings_of(node, BashConfig)
    services = ctx.services
    evaluator = services.evaluator
    has_error_way = "error" in ctx.connected.get(node["id"], frozenset())
    seconds = max(1.0, s.timeout_seconds)
    variables: dict[str, str] = {}
    for row in s.env:
        name = row.name.strip()
        if not VARIABLE.fullmatch(name):
            raise AgentBuildError(
                f"{where_of(node)}: its environment variable {name!r} isn't a name bash reads: "
                "letters, digits and _, not starting with a digit."
            )
        if name == WORKSPACE_VARIABLE:
            raise AgentBuildError(
                f"{where_of(node)}: {WORKSPACE_VARIABLE} is the run's folder; call its variable something else."
            )
        if name in variables:
            raise AgentBuildError(f"{where_of(node)}: two of its environment variables are called {name}.")
        variables[name] = row.value

    async def command(data: dict[str, Any], workspace: str) -> dict[str, Any]:
        cwd = await evaluator.render(s.cwd, data, what="Its folder")
        folder = await asyncio.to_thread(folder_in, workspace, cwd)
        env = {**services.command_environment, **UNASKED}
        for name, value in variables.items():
            env[name] = await evaluator.render(value, data, what=f"Its {name} variable")
        max_bytes = services.max_command_output_bytes
        if services.command_sandbox is not None:
            finished = await services.command_sandbox.run(
                s.command, workspace=workspace, folder=folder, env=env, seconds=seconds, max_bytes=max_bytes
            )
        else:
            env[WORKSPACE_VARIABLE] = workspace
            finished = await run_command(s.command, cwd=folder, env=env, seconds=seconds, max_bytes=max_bytes)
        if finished.exit_code != 0:
            raise StepFailed(
                _exited(finished),
                details={"exit_code": finished.exit_code, "stdout": finished.stdout, "stderr": finished.stderr},
            )
        return {
            "exit_code": finished.exit_code,
            "stdout": finished.stdout,
            "stderr": finished.stderr,
            "cwd": str(folder),
            "truncated": finished.truncated,
        }

    async def run(adk: Context, node_input: Any) -> Event:
        if not services.commands:
            # The run checks this before it starts (runs.AdkRun.run).
            raise RunFailed(
                f"{label_of(node)} failed: this worker doesn't run commands: {NO_COMMANDS}", step=node["id"]
            )
        workspace = services.workspace
        if workspace is None:
            raise RunFailed(
                f"{label_of(node)} failed: the run has no folder to work in: "
                "turn on Allow file system on the workflow's start",
                step=node["id"],
            )
        try:
            output = await command(ctx.data(adk, node_input), workspace)
        except StepFailed as error:
            if not has_error_way:
                raise failed(node, error) from None
            return Event(route="error", custom_metadata={ERROR_KEY: error.as_error()})  # type: ignore[call-arg]  # ADK's shorthand for actions.route
        return Event(output=output, route="success")  # type: ignore[call-arg]  # ADK's shorthand for actions.route

    return FunctionNode(name=adk_name(text(node.get("name"))), func=run)


def _exited(finished: Finished) -> str:
    """:return: How a command that failed ended, and the last thing it said."""
    code = finished.exit_code
    how = f"It exited with {code}" if code >= 0 else f"It was killed by signal {-code}"
    said = last_line(finished.stderr) or last_line(finished.stdout)
    return f"{how}: {said}" if said else how


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
