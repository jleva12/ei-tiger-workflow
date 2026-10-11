"""The code tools: the code graph's tools, the very ones the code graph MCP
server serves (``forge_codegraph.tools``), for the assistant, as the person
it's talking to.

Each tool takes the organization whose code it reads (``organization_id``)
beside the MCP tool's own arguments. Before a call, the tool asks the admin
API's own route what the person reads there
(``/organizations/{id}/code-graph/access``, as the person, authorized in the
organization: ``repositories:read``), the same answer the MCP server gets for
a credential, and the tool then reads only those repositories, follows only
the organization's cross-repository links, and describes only its systems.
The answer is kept a little while per person and organization.

Every tool is read-only. As a :class:`ForgeBaseToolset`, none raises: a
refusal answers ``failed`` with the reason and how to go on; a failure the
graph explains (a bad argument, a node that isn't there) says so; anything
else is logged and masked.
"""

import copy
import inspect
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Any, get_args, get_origin

from fastapi import FastAPI
from forge_codegraph import INSTRUCTIONS as CODE_INSTRUCTIONS
from forge_codegraph import TOOLS, CodeTools, RepositoryScope
from forge_codegraph_query import CodeGraph, GraphError
from forge_common.adk import ToolFailure
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from pydantic import Field
from pydantic.fields import FieldInfo

from forge_admin.assistant.person_api import PersonApi, Refusal
from forge_admin.assistant.route_tools import ApiRefused, RouteToolset, uuid_arg

logger = logging.getLogger(__name__)

TOOL_NAMES = frozenset(TOOLS)
# Seconds what the person reads in an organization is kept.
SCOPE_SECONDS = 30.0
# Seconds a tool may take: impact and change_impact walk far.
TOOL_TIMEOUT = 120.0

# Added to the agent's instruction while it has these tools: the MCP
# server's own instructions, and what's different here.
INSTRUCTION = f"""
Code:
{CODE_INSTRUCTIONS}
- Every tool takes organization_id: the organization whose code \
repositories it reads, from the page or the request. It reads only those \
the person may read there (repositories:read).
- Repository ids are the code graph's (repo:...), from list_repositories, \
repository_connections or system_design; never a code repository's ID from \
the page (a UUID): match it by owner/name in list_repositories.
- Answer from what the tools return: name the declarations with their \
qualified names and say where they are (file path and line), and quote \
code only as read_source or read_file returned it. When nothing matches, \
say so; never describe code you haven't read.
"""

ORGANIZATION = inspect.Parameter(
    "organization_id",
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    annotation=Annotated[
        str,
        Field(
            description="The organization's ID (a UUID), from the page or the "
            "request: the tools read its code repositories"
        ),
    ],
)
CONTEXT = inspect.Parameter(
    "tool_context", inspect.Parameter.KEYWORD_ONLY, annotation=ToolContext
)

# What the call being served reads: set by each tool before it runs.
_SCOPE: ContextVar[RepositoryScope] = ContextVar("code_scope")


class CodeToolset(RouteToolset):
    """
    The code graph's tools over an organization's code repositories, as the
    person the assistant is talking to. None changes anything.

    :param api: The admin API, called as the person for what they read.
    :param graph: The code graph (``app.state.codegraph``).
    :param clock: For tests: seconds, monotonic.
    :param kwargs: :class:`ForgeBaseToolset`'s (``max_result_chars``, ...).
    """

    STATUS_FIXES = {
        403: [
            "The person can't read this organization's code repositories: that "
            "needs repositories:read there. Tell them, and who can give it; don't "
            "retry."
        ],
        404: [
            "Check the IDs: the organization's ID from the page or the request, "
            "repository ids from list_repositories, node ids from earlier results."
        ],
    }

    def __init__(
        self,
        api: PersonApi,
        graph: CodeGraph,
        *,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("timeout_seconds", TOOL_TIMEOUT)
        super().__init__(api, **kwargs)
        self._code = CodeTools(graph, _SCOPE.get)
        self._clock = clock
        self._kept: dict[tuple[str, str], tuple[float, RepositoryScope]] = {}
        self._tools: list[BaseTool] = [
            self._tool(name, method) for name, _, method in self._code.tools()
        ]

    @classmethod
    def tool_names(cls) -> frozenset[str]:
        return TOOL_NAMES

    def timeout_for(self, tool: BaseTool) -> float | None:
        return TOOL_TIMEOUT

    def _tool(self, name: str, method: Callable[..., Any]) -> FunctionTool:
        """``method`` as an ADK tool: its arguments and description, and the
        organization whose code it reads."""
        signature = inspect.signature(method)

        async def run(
            organization_id: str, tool_context: ToolContext, **arguments: Any
        ) -> Any:
            scope = await self._scope(tool_context, organization_id)
            token = _SCOPE.set(scope)
            try:
                return await method(**arguments)
            except GraphError as error:
                raise ToolFailure(str(error)) from None
            except ToolFailure:
                raise
            except Exception:
                logger.exception("The code tool %s failed", name)
                raise ToolFailure(
                    "The code graph couldn't answer; try again."
                ) from None
            finally:
                _SCOPE.reset(token)

        parameters = [
            ORGANIZATION,
            *(_without_alias(p) for p in signature.parameters.values()),
            CONTEXT,
        ]
        run.__name__ = run.__qualname__ = name
        run.__doc__ = method.__doc__
        run.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
        run.__annotations__ = {p.name: p.annotation for p in parameters} | {
            "return": signature.return_annotation
        }
        return FunctionTool(run)

    async def _scope(
        self, tool_context: ToolContext, organization_id: str
    ) -> RepositoryScope:
        """
        What the person reads in the organization, as the admin API answers.

        :raises ToolFailure: There's no person, or the ID isn't one.
        :raises ApiRefused: The API refused: not theirs to read.
        """
        user_id = tool_context.user_id
        if not user_id:
            raise ToolFailure("No signed-in person to act as.")
        organization = uuid_arg(organization_id, "organization_id")
        now = self._clock()
        key = (user_id, organization)
        kept = self._kept.get(key)
        if kept is not None and kept[0] > now:
            return kept[1]
        try:
            answer = await self._api.call(
                user_id, "GET", f"/organizations/{organization}/code-graph/access"
            )
        except Refusal as refusal:
            raise ApiRefused(refusal) from None
        scope = RepositoryScope.from_access(answer)
        self._kept = {k: v for k, v in self._kept.items() if v[0] > now}
        self._kept[key] = (now + SCOPE_SECONDS, scope)
        return scope


def _without_alias(parameter: inspect.Parameter) -> inspect.Parameter:
    """A parameter the model names as Python does: an argument the MCP
    server takes by another name (path's ``from``) is ``from_id`` here, as
    no Python parameter can be named ``from``."""
    annotation = parameter.annotation
    if get_origin(annotation) is not Annotated:
        return parameter
    base, *metadata = get_args(annotation)
    plain = []
    for item in metadata:
        if isinstance(item, FieldInfo) and item.alias:
            item = copy.copy(item)
            item.alias = item.validation_alias = item.serialization_alias = None
            item.alias_priority = None
        plain.append(item)
    return parameter.replace(annotation=Annotated[(base, *plain)])  # type: ignore[valid-type]


def toolset(app: FastAPI, **kwargs: Any) -> CodeToolset | None:
    """
    The code tools, reading ``app``'s code graph as each conversation's
    person.

    :param app: The admin API's application.
    :param kwargs: :class:`CodeToolset`'s options.
    :return: The toolset, or None without ``jwt_secret``, which the person's
        tokens are signed with, or without the code graph.
    """
    if app.state.settings.jwt_secret is None:
        logger.warning(
            "FORGE_ADMIN_JWT_SECRET isn't set: the assistant can't read code as the "
            "people it talks to"
        )
        return None
    graph: CodeGraph | None = getattr(app.state, "codegraph", None)
    if graph is None:
        return None
    return CodeToolset(PersonApi.of_app(app), graph, **kwargs)
