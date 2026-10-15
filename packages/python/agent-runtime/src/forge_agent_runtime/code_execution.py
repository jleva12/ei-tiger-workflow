"""Running code: a Code execution tool (a ``code_execution`` node).

The model writes Python and calls the tool with it; a code executor (ADK's
``BaseCodeExecutor``) runs it, and the tool answers with what it printed and
the errors it raised. ADK's own way, an agent's ``code_executor``, runs the
code blocks in the model's answer instead: a model other than Gemini isn't
told how, a ```python block an answer shows would run, and neither the chat
nor a run's trace shows what ran. As a tool, it's a call like any other:
every model makes one, and it's shown, journaled and confirmed as the others
are.

What runs the code is the deployment's (``RuntimeServices.code_executor``,
:func:`code_executor`), as its sandbox mode says (``forge_common.sandbox``):
ADK's ``UnsafeLocalCodeExecutor`` (``local``), a new Python process for each
call on the same machine as the runtime, with its environment, its files and
its network, for local development only; or a new NVIDIA OpenShell sandbox
for each call (``openshell``), confined by its policy. An agent that runs
code where it's ``off`` doesn't build.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import CodeExecutionInput
from google.adk.code_executors.unsafe_local_code_executor import UnsafeLocalCodeExecutor
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from forge_common.adk import ToolFailure
from forge_common.sandbox import OpenShellSettings, SandboxUnavailable

#: The most seconds one call's code may run before it's stopped.
CODE_SECONDS = 60

DESCRIPTION = (
    "Runs Python 3 code and answers with what it printed (stdout) and its errors (stderr). Use it to "
    "calculate, transform data or check something by running it, rather than working it out in your "
    "head. Each call runs in a fresh process: nothing (variables, imports) carries over from one call "
    "to the next, so send the whole program, and print() what you need to see."
)


def code_executor(mode: str, openshell: OpenShellSettings | None = None) -> BaseCodeExecutor | None:
    """
    :param mode: A deployment's ``SandboxMode``.
    :param openshell: For ``openshell``: the gateway, and the sandboxes it
        makes; the CLI's active gateway and its default image without.
    :return: What runs agents' code, each call stopped after
        :data:`CODE_SECONDS`: ADK's ``UnsafeLocalCodeExecutor`` for
        ``local``, an ``OpenShellCodeExecutor`` for ``openshell``; None for
        ``off``.
    :raises ValueError: ``openshell``'s policy file can't be used.
    :raises ImportError: ``openshell`` without ``forge-agent-runtime[openshell]``.
    """
    if mode == "local":
        return UnsafeLocalCodeExecutor(timeout_seconds=CODE_SECONDS)
    if mode == "openshell":
        try:
            from forge_common.sandbox.openshell import OpenShellCodeExecutor
        except ImportError as error:
            raise ImportError(
                "Running agents' code in OpenShell needs the openshell SDK: install forge-agent-runtime[openshell]."
            ) from error
        return OpenShellCodeExecutor(settings=openshell or OpenShellSettings(), timeout_seconds=CODE_SECONDS)
    return None


class CodeTool(BaseTool):
    """Runs the Python the model sends on a code executor, and answers with
    what it printed and its errors."""

    def __init__(self, *, name: str, executor: BaseCodeExecutor) -> None:
        super().__init__(name=name, description=DESCRIPTION)
        self.executor = executor

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python program to run, whole.",
                    }
                },
                "required": ["code"],
            },
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ToolFailure("Send the Python program to run in code.")
        # The executor blocks until its process ends (or is stopped): off the event loop.
        try:
            result = await asyncio.to_thread(
                self.executor.execute_code,
                tool_context.get_invocation_context(),
                CodeExecutionInput(code=code),
            )
        except SandboxUnavailable as error:
            raise ToolFailure(
                f"The code didn't run: {error}",
                [
                    "Don't send it again now: work it out without running code, or tell the person code can't run."
                ],
            ) from error
        return {"stdout": result.stdout, "stderr": result.stderr}
