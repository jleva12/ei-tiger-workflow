"""An agent's MCP tools check their results against their output schemas on
the bounded engine (forge_common.mcp) rather than with the mcp client's own
check, which can freeze the process: whether the runtime makes the toolset (a
server by URL) or the hosted runtime's MCP servers do (one of the
organization's)."""

import copy
from typing import Any

import pytest
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from mcp.client.session import ClientSession

from forge_agent_runtime.build import build_app
from forge_agent_runtime.document import parse_document
from forge_agent_runtime.services import RuntimeServices
from forge_common.mcp import _validate_tool_result
from tests.conftest import EXAMPLE_HOSTS


class OrganizationServer(BaseToolset):
    """One of the organization's MCP servers, as the hosted runtime connects it."""

    async def get_tools(self, readonly_context: Any = None) -> list[BaseTool]:
        return []


async def organization_servers(config: Any, *, organization_id: str | None) -> BaseToolset:
    return OrganizationServer()


@pytest.mark.parametrize("server", ["", "mcp_1"], ids=["by URL", "the organization's"])
async def test_an_mcp_tool_checks_its_results_on_the_bounded_engine(
    example, models, web, monkeypatch: pytest.MonkeyPatch, server: str
) -> None:
    async def clients_own(self: ClientSession, name: str, result: Any) -> None:
        raise AssertionError("the client's own check")

    monkeypatch.setattr(ClientSession, "validate_tool_result", clients_own)
    doc = copy.deepcopy(example)
    next(n for n in doc["nodes"] if n["id"] == "help")["config"]["server"] = server

    await build_app(
        parse_document(doc),
        models=models,
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS, mcp_servers=organization_servers),
        http=web(),
    )

    assert ClientSession.validate_tool_result is _validate_tool_result
