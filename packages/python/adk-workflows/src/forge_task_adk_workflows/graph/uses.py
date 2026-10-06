"""
What an ADK workflow's LLM agents use beyond its document: agents from the
Agents page (an ``llm`` node whose ``source`` is ``agent``, or an agent
tool), the organization's knowledge bases and MCP servers, and other
workflows called as tools. The admin API finds them as a run starts, to
check them and carry what the run needs in its payload; the worker builds
from that.

An agent from the Agents page is named as its runtime names it:
``ca_x`` (its latest published version, as the run starts), ``ca_x@3``, or
``ca_x@draft`` (:func:`agent_ref`); so is another workflow (``ag_x``), run by
a ``saved`` node or called as a tool.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from forge_task_adk_workflows.graph.factories.base import config_of, items, text


@dataclass(frozen=True)
class Tool:
    """
    One of an LLM agent's tools, and where it is.

    :ivar where: The node (and sub-agent) it's on, for a refusal: ``Node 'Triage'``.
    :ivar kind: ``mcp``, ``knowledge_base``, ``http_tool``, ``openapi``,
        ``saved_agent`` or ``adk_workflow``.
    """

    where: str
    id: str
    kind: str
    name: str
    config: dict[str, Any]


@dataclass(frozen=True)
class AgentUse:
    """An LLM node that is an agent from the Agents page, and which version."""

    where: str
    agent: str
    version: int | str | None

    @property
    def ref(self) -> str:
        return agent_ref(self.agent, self.version)


def agent_ref(agent: str, version: Any) -> str:
    """:return: How an agent and its version are named: ``ca_x``, ``ca_x@3``, ``ca_x@draft``."""
    if version is None or version == "":
        return agent
    return f"{agent}@{version}"


def _where(node: dict[str, Any], path: list[str]) -> str:
    where = f"Node '{text(node.get('name')) or text(node.get('id'))}'"
    return where + "".join(f", sub-agent '{name}'" for name in path)


def _tools_of(item: dict[str, Any], where: str) -> Iterator[Tool]:
    for tool in items(config_of(item).get("tools")):
        if isinstance(tool, dict):
            yield Tool(
                where=f"{where}, tool '{text(tool.get('name')) or text(tool.get('id'))}'",
                id=text(tool.get("id")),
                kind=text(tool.get("kind")),
                name=text(tool.get("name")),
                config=config_of(tool),
            )


def _within(item: dict[str, Any], node: dict[str, Any], path: list[str]) -> Iterator[Tool]:
    yield from _tools_of(item, _where(node, path))
    for sub in items(config_of(item).get("sub_agents")):
        if isinstance(sub, dict):
            yield from _within(sub, node, [*path, text(sub.get("name")) or text(sub.get("id"))])


def tools_in(document: dict[str, Any]) -> list[Tool]:
    """:return: Every tool of the document's LLM agents (nodes and sub-agents), in order."""
    found: list[Tool] = []
    for node in items(document.get("nodes")):
        if isinstance(node, dict):
            found.extend(_within(node, node, []))
    return found


def agents_in(document: dict[str, Any]) -> list[AgentUse]:
    """:return: The document's LLM nodes that are agents from the Agents page."""
    found: list[AgentUse] = []
    for node in items(document.get("nodes")):
        if not isinstance(node, dict) or node.get("kind") != "llm":
            continue
        config = config_of(node)
        if config.get("source") == "agent":
            version = config.get("version")
            found.append(
                AgentUse(
                    where=_where(node, []),
                    agent=text(config.get("agent")),
                    version=version if isinstance(version, (int, str)) else None,
                )
            )
    return found


def workflows_in(document: dict[str, Any]) -> list[str]:
    """
    :return: The workflows the document's LLM agents call as tools, by
        reference (``ag_x``, ``ag_x@3``, ``ag_x@draft``), in order.
    """
    found: list[str] = []
    for tool in tools_in(document):
        workflow = text(tool.config.get("workflow"))
        if tool.kind != "adk_workflow" or not workflow:
            continue
        ref = agent_ref(workflow, tool.config.get("version"))
        if ref not in found:
            found.append(ref)
    return found


def saved_in(document: dict[str, Any]) -> list[str]:
    """
    :return: The workflows the document's ``saved`` nodes run, by reference
        (``ag_x``, ``ag_x@3``, ``ag_x@draft``), in order.
    """
    found: list[str] = []
    for node in items(document.get("nodes")):
        if not isinstance(node, dict) or node.get("kind") != "saved":
            continue
        config = config_of(node)
        agent = text(config.get("agent"))
        ref = agent_ref(agent, config.get("version")) if agent else ""
        if ref and ref not in found:
            found.append(ref)
    return found


def bare_id(ref: str) -> str:
    """:return: The agent or workflow a reference names, without its version."""
    return ref.partition("@")[0]
