"""
The kinds of node the web console's builders offer, the same for everyone
signed in: a workflow's (the ADK workflows task's node catalog,
``forge_task_adk_workflows.graph.catalog``) and a chat agent's on the Agents
canvas, whose tools a workflow's LLM agents use too
(``forge_agent_runtime.catalog``).
"""

from fastapi import APIRouter, Depends
from forge_agent_runtime.catalog import CHAT_NODE_CATALOG, ChatNodeCatalog
from forge_task_adk_workflows.graph import NODE_CATALOG, NodeCatalog

from forge_admin.auth.access import current_user

router = APIRouter(tags=["node kinds"], dependencies=[Depends(current_user)])


@router.get("/workflow-node-kinds", summary="The kinds of workflow node")
async def workflow_node_kinds() -> NodeCatalog:
    """
    Every kind of node a workflow can have, in the builder's library order,
    with the library's groups: each kind's words and icon, its ways out and
    the settings a new one starts with. Anyone signed in (or an API key) may
    read it.
    \f
    :return: The workflow node catalog.
    :raises HTTPException: 401 without a signed-in user.
    """
    return NODE_CATALOG


@router.get("/chat-agent-node-kinds", summary="The kinds of node on the Agents canvas")
async def chat_agent_node_kinds() -> ChatNodeCatalog:
    """
    Every kind of node a chat agent can have, in the Agents canvas's library
    order, with its groups: each kind's words and icon, its ways out (an
    agent's tools and who it hands off to), whether it can be handed off to,
    whether a workflow's LLM agent may have it as a tool, and the settings a
    new one starts with. Anyone signed in (or an API key) may read it.
    \f
    :return: The chat agent node catalog.
    :raises HTTPException: 401 without a signed-in user.
    """
    return CHAT_NODE_CATALOG
