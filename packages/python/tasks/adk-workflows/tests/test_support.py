"""What the graph's nodes are built on: the HTTP guard and helpers, and the
models LLM nodes run on without a model provider configuration."""

import httpx
import pytest

from forge_task_adk_workflows.config import AdkWorkflowsSettings
from forge_task_adk_workflows.models import DEFAULT_GEMINI_MODEL, gemini_config, load_models
from forge_task_adk_workflows.support.errors import StepFailed
from forge_task_adk_workflows.support.http import check_url, parse_body, read_body


async def test_http_requests_never_reach_a_private_network() -> None:
    for url in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
    ):
        with pytest.raises(StepFailed, match="private network"):
            await check_url(url, allow_private=False, allowed_hosts=[])
    with pytest.raises(StepFailed, match="must start with http"):
        await check_url("file:///etc/passwd", allow_private=False, allowed_hosts=[])
    with pytest.raises(StepFailed, match="has no host"):
        await check_url("http:///x", allow_private=False, allowed_hosts=[])
    assert await check_url("http://127.0.0.1/x", allow_private=True, allowed_hosts=[]) == "http://127.0.0.1/x"
    assert await check_url("http://Intranet/x", allow_private=False, allowed_hosts=["intranet"]) == "http://Intranet/x"


async def test_a_response_is_read_up_to_its_limit() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 10))
    ) as client:
        async with client.stream("GET", "https://example.com") as response:
            assert await read_body(response, 10) == b"x" * 10
        async with client.stream("GET", "https://example.com") as response:
            with pytest.raises(StepFailed, match="over 9 bytes"):
                await read_body(response, 9)


def test_a_body_is_json_when_it_says_so_or_looks_it() -> None:
    assert parse_body(b'{"a": 1}', "application/json") == {"a": 1}
    assert parse_body(b"[1, 2]", "text/plain") == [1, 2]
    assert parse_body(b"{not json", "application/json") == "{not json"
    assert parse_body(b"plain", "text/plain") == "plain"


def test_without_a_configuration_llm_nodes_run_on_gemini() -> None:
    config = gemini_config("key")
    assert (config.default.provider, config.default.model) == ("google", DEFAULT_GEMINI_MODEL)
    [model] = config.providers["google"].models
    assert (model.id, model.name) == (DEFAULT_GEMINI_MODEL, "Gemini 3.5 Flash")
    assert gemini_config("key", "gemini-test").default.model == "gemini-test"

    models = load_models(AdkWorkflowsSettings(google_api_key="key", default_model="gemini-test"))
    assert models is not None
    assert models.default.ref == "google/gemini-test"
    assert load_models(AdkWorkflowsSettings()) is None
