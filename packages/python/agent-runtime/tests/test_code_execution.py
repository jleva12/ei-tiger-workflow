import copy
from typing import Any, cast

import pytest
from google.adk.agents import LlmAgent
from google.adk.code_executors.unsafe_local_code_executor import UnsafeLocalCodeExecutor

from forge_agent_runtime.build import BuildError, build_app, build_tools
from forge_agent_runtime.code_execution import CODE_SECONDS, CodeTool, code_executor
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.services import RuntimeServices
from forge_agent_runtime.tools import GuardedTools
from forge_common.adk import ToolFailure
from tests.conftest import EXAMPLE_HOSTS


class Context:
    """What a tool's call is given, as much as running code reads of it."""

    def get_invocation_context(self) -> None:
        return None


CONTEXT: Any = Context()


def with_code(example: dict[str, Any], **config: Any) -> dict[str, Any]:
    """The example, its agent able to run code."""
    doc = copy.deepcopy(example)
    doc["nodes"].append(
        {"id": "code", "kind": "code_execution", "name": "Run code", "config": config, "outputs": []}
    )
    doc["edges"].append(
        {"id": "agent:tools->code", "source": "agent", "source_output": "tools", "target": "code"}
    )
    return doc


def test_code_runs_on_this_machine_only_when_the_deployment_says_so():
    assert code_executor("off") is None
    local = code_executor("local")
    assert isinstance(local, UnsafeLocalCodeExecutor)
    assert local.timeout_seconds == CODE_SECONDS


async def test_an_agent_with_code_execution_gets_a_tool_that_runs_its_code(example, models, web):
    services = RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, code_executor=code_executor("local"))
    app = await build_app(parse_document(with_code(example)), models=models, services=services, http=web())
    root = app.root_agent
    assert isinstance(root, LlmAgent)
    guarded = root.tools[-1]
    assert isinstance(guarded, GuardedTools) and not guarded.confirm
    [tool] = await guarded.get_raw_tools()
    assert isinstance(tool, CodeTool) and tool.name == "run_code"
    declaration = tool._get_declaration()
    assert declaration is not None
    assert cast(dict[str, Any], declaration.parameters_json_schema)["required"] == ["code"]

    # A person confirms each run when it says so.
    asking = await build_app(
        parse_document(with_code(example, confirm=True)), models=models, services=services, http=web()
    )
    root = asking.root_agent
    assert isinstance(root, LlmAgent) and isinstance(root.tools[-1], GuardedTools)
    [tool] = await root.tools[-1].get_raw_tools()
    assert await tool.check_require_confirmation({"code": "print(1)"}, CONTEXT) is True


async def test_an_agent_with_code_execution_doesnt_build_where_its_off(example, models, web):
    with pytest.raises(BuildError, match="code execution is off"):
        await build_app(
            parse_document(with_code(example)),
            models=models,
            services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
            http=web(),
        )


async def test_a_workflows_agent_gets_the_tool_on_its_own(models):
    [guarded] = await build_tools(
        "Calculator",
        "code_execution",
        {},
        models=models,
        services=RuntimeServices(code_executor=code_executor("local")),
    )
    assert isinstance(guarded, GuardedTools)
    [tool] = await guarded.get_raw_tools()
    assert tool.name == "calculator"
    with pytest.raises(BuildError, match="code execution is off"):
        await build_tools("Calculator", "code_execution", {}, models=models)


async def test_the_tool_answers_with_what_the_code_printed_and_its_errors():
    tool = CodeTool(name="run_code", executor=UnsafeLocalCodeExecutor(timeout_seconds=CODE_SECONDS))
    ran = await tool.run_async(args={"code": "print(sum(range(5)))"}, tool_context=CONTEXT)
    assert ran == {"stdout": "10\n", "stderr": ""}

    failed = await tool.run_async(args={"code": "print('so far')\n1 / 0"}, tool_context=CONTEXT)
    assert failed["stdout"] == "so far\n"
    assert "ZeroDivisionError" in failed["stderr"]

    with pytest.raises(ToolFailure, match="Python program"):
        await tool.run_async(args={"code": "  "}, tool_context=CONTEXT)
