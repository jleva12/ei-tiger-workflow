import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import cast

import pytest
from google.adk.agents.readonly_context import ReadonlyContext

from forge_agent_runtime import templating
from forge_agent_runtime.http_templates import chat_values
from forge_agent_runtime.templating import TemplateError, evaluate_async, instruction_provider


def test_pathological_regex_is_bounded_even_inside_a_single_jsonata_operation():
    # A subprocess also keeps a regression to Python re from wedging pytest itself.
    code = """
from forge_agent_runtime.templating import TemplateError, evaluate
try:
    evaluate('$match(value, /^(a|aa)+$/)', {'value': 'a' * 80 + '!'}, timeout_ms=20)
except TemplateError as error:
    assert 'longer than 20 ms' in str(error), str(error)
else:
    raise AssertionError('The pathological regex was not timed out')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


async def test_regex_evaluation_leaves_the_event_loop_responsive():
    completed = []

    async def tick():
        await asyncio.sleep(0.01)
        completed.append("tick")

    ticker = asyncio.create_task(tick())
    with pytest.raises(TemplateError, match="longer than"):
        await evaluate_async("$match(value, /^(a|aa)+$/)", {"value": "a" * 80 + "!"}, timeout_ms=100)
    completed.append("evaluation")
    await ticker
    assert completed == ["tick", "evaluation"]


async def test_instructions_and_http_templates_evaluate_on_a_worker_thread(monkeypatch):
    event_loop_thread = threading.get_ident()
    threads = []
    original = templating.evaluate

    def record(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(templating, "evaluate", record)
    provider = instruction_provider("Hello {{ state.name }}", what="Test agent")
    context = cast(ReadonlyContext, SimpleNamespace(state={"name": "Ada"}))
    assert await provider(context) == "Hello Ada"
    assert await chat_values(None, what="HTTP URL")("1 + 2") == 3
    assert len(threads) == 2 and all(thread != event_loop_thread for thread in threads)


def test_template_regex_flags_and_replacement_semantics_are_preserved():
    assert templating.evaluate('$replace("AbC", /b/i, "x")', {}) == "AxC"
    with pytest.raises(TemplateError, match="doesn't compile"):
        templating.evaluate('$match("x", /((a{1000}){1000}){1000}/)', {})
