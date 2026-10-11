"""The code graph's tools over MCP, every one read-only.

The tools themselves are ``forge_codegraph.tools``': one definition each,
which the admin API's assistant offers too, so a question about code answers
alike in both. Each tool's docstring is its description, as the Go server's
was.

Answers are JSON objects. Failures a caller can act on (a bad argument, a node
that isn't there, a stale cursor) come back as tool errors the model reads;
anything else is logged and masked.

Each caller reads only its organization's repositories (access.py), as the
admin API answered for its credential.
"""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool as FastMCPTool
from forge_codegraph import CodeTools, RepositoryScope
from forge_codegraph_query import CodeGraph
from forge_codegraph_query.errors import GraphError
from mcp.types import ToolAnnotations

from forge_codegraph_mcp.access import current_scope
from forge_codegraph_mcp.core.settings import Settings
from forge_codegraph_mcp.tools.base import Toolset

type Answer = dict[str, Any]


def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, idempotent_hint=True)


def _answers[**P](fn: Callable[P, Awaitable[Answer]]) -> Callable[P, Awaitable[Answer]]:
    """Turns the graph's expected failures into tool errors the model reads,
    logged without a traceback."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Answer:
        try:
            return await fn(*args, **kwargs)
        except GraphError as exc:
            raise ToolError(str(exc), log_level=logging.WARNING) from exc

    return wrapper


class CodeGraphTools(Toolset):
    """Explore, search and walk the code graph, and read exact source."""

    def __init__(
        self,
        settings: Settings,
        graph: CodeGraph,
        scope: Callable[[], RepositoryScope] = current_scope,
    ) -> None:
        """
        :param graph: The code graph, read as every app reads it.
        :param scope: The repositories the request being served reads: by
            default its credential's, as the admin API answered.
        """
        super().__init__(settings)
        self.tools = CodeTools(graph, scope)

    def register(self, server: FastMCP) -> None:
        for name, title, tool in self.tools.tools():
            server.add_tool(
                FastMCPTool.from_function(_answers(tool), name=name, annotations=_read_only(title))
            )
