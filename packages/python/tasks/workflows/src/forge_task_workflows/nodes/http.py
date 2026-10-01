"""HTTP request: calls a URL and hands on the response.

The URL and headers are text with ``{{ }}`` in them; the body is a JSONata
expression (an object or list is sent as JSON, text as it is). A 2xx takes
Success; anything else, or no answer after its retries, takes Error with the
status. With a declared output, a 2xx whose body doesn't fit it takes Error
too. Private addresses are refused (services/http_guard.py), redirects
included.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

import httpx

from forge_task_workflows.engine import Outcome, StepRun
from forge_task_workflows.errors import StepFailed
from forge_task_workflows.nodes.basic import fit_problems
from forge_task_workflows.services.http_guard import check_url

MAX_REDIRECTS = 5
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


async def http(step: StepRun) -> Outcome:
    s = step.settings
    url = step.render(s.url, what="Its URL").strip()
    headers = {h.name.strip(): step.render(h.value, what=f"Its {h.name} header") for h in s.headers if h.name.strip()}
    request: dict[str, Any] = {}
    if s.method != "GET" and s.body.strip():
        body = step.evaluate(s.body, what="Its body")
        if isinstance(body, dict | list):
            request["json"] = body
        elif body is not None:
            request["content"] = body if isinstance(body, str) else json.dumps(body)
    attempts = 1 + max(0, s.retries)
    last: StepFailed | None = None
    for attempt in range(attempts):
        if attempt:
            await step.services.sleep(min(2**attempt, 30))
        try:
            response = await _send(step, s.method, url, headers, request, timeout=s.timeout_seconds)
        except StepFailed as failed:
            last = failed
            if failed.status is None or failed.status in RETRYABLE:
                continue
            raise
        if 200 <= response["status"] < 300:
            problems = fit_problems(response["body"], s.output_schema, "response") if s.output_schema else []
            if problems:
                raise StepFailed(
                    "Its response doesn't fit its declared output: " + "; ".join(problems),
                    status=response["status"],
                    details={"body": response["body"]},
                )
            return Outcome("success", response)
        last = StepFailed(
            f"It answered {response['status']}", status=response["status"], details={"body": response["body"]}
        )
        if response["status"] not in RETRYABLE:
            break
    assert last is not None
    raise last


async def _send(
    step: StepRun, method: str, url: str, headers: dict[str, str], request: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    settings = step.services.settings
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url, allow_private=settings.http_allow_private, allowed_hosts=settings.http_allowed_hosts)
        try:
            async with step.services.http.stream(
                method, url, headers=headers, timeout=timeout, follow_redirects=False, **request
            ) as response:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    if response.status_code in (301, 302, 303):
                        method, request = "GET", {}
                    continue
                raw = await _read(response, settings.http_max_response_bytes)
                return {
                    "status": response.status_code,
                    "headers": {k.lower(): v for k, v in response.headers.items()},
                    "body": _parse(raw, response.headers.get("content-type", "")),
                }
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise StepFailed(f"{url} didn't answer: {type(exc).__name__}: {exc}") from exc
        except TimeoutError as exc:
            raise StepFailed(f"{url} didn't answer in {timeout:g} seconds") from exc
    raise StepFailed(f"{url} redirected more than {MAX_REDIRECTS} times")


async def _read(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise StepFailed(f"Its response is over {limit:,} bytes, the most an HTTP step reads")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text
