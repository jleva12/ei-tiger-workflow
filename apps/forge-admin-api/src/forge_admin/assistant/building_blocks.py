"""What an organization has for the builders' agents and steps to use, as the
assistant's builder specialists read it: its agents from the Agents page,
its ADK workflows, credentials, knowledge bases, MCP servers, skills, agent
permissions, team UI components and the models agents run on.

Both builders' toolsets (``workflow_tools.py``, ``agent_tools.py``) list
them through the web console's own routes, as the person: a list they may
not read says why instead.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from forge_admin.assistant.route_tools import ApiRefused, pick, uuid_arg

#: Calls a route as the person: method, path.
Call = Callable[[str, str], Awaitable[Any]]

#: The sections every builder gets, and those only some ask for.
SECTIONS = (
    "agents",
    "workflows",
    "credentials",
    "knowledge_bases",
    "mcp_servers",
    "skills",
)
#: The agent permissions an agent's access rule names: the Agents page's.
ACCESS = "access_permissions"
#: The team UI components Rich answers may pick by key: the Agents page's.
UI_COMPONENTS = "ui_components"


async def building_blocks(
    call: Call,
    organization_id: str,
    *,
    access: bool = False,
    ui_components: bool = False,
) -> dict[str, Any]:
    """
    :param call: Calls a route as the person.
    :param organization_id: The organization, by ID.
    :param access: Also list its agent permissions, which agents' access
        rules name.
    :param ui_components: Also list its team UI components agents may use
        (published, enabled), which Rich answers nodes pick by key.
    :return: Each list by kind, and the models; a list the person may not
        read says why.
    """
    org = uuid_arg(organization_id, "organization_id")
    sections: dict[str, tuple[str, Callable[[Mapping[str, Any]], Any]]] = {
        "agents": (f"/organizations/{org}/chat-agents", agent_block),
        "workflows": (f"/organizations/{org}/agents", workflow_block),
        "credentials": (f"/organizations/{org}/credentials", credential_block),
        "knowledge_bases": (f"/organizations/{org}/knowledge-bases", knowledge_block),
        "mcp_servers": (f"/organizations/{org}/mcp-servers", mcp_block),
        "skills": (
            f"/organizations/{org}/skills",
            lambda skill: pick(skill, ("id", "name", "description")),
        ),
    }
    if access:
        sections[ACCESS] = (
            f"/organizations/{org}/agent-access/permissions",
            lambda permission: pick(permission, ("id", "key", "description", "groups")),
        )
    found: dict[str, Any] = {}
    for name, (path, shape) in sections.items():
        try:
            records = await call("GET", path)
            found[name] = [shape(record) for record in records or []]
        except ApiRefused as refused:
            found[name] = {"unavailable": refused.reason}
    if ui_components:
        try:
            catalog = await call("GET", f"/organizations/{org}/ui-components/catalog")
            found[UI_COMPONENTS] = [
                ui_component_block(record)
                for record in (catalog or {}).get("team") or []
            ]
        except ApiRefused as refused:
            found[UI_COMPONENTS] = {"unavailable": refused.reason}
    try:
        models = await call("GET", "/agents/apps/forge/models")
        found["models"] = {
            "default": (models or {}).get("defaultModel"),
            "choices": [
                {
                    "provider": model.get("provider"),
                    "name": model.get("model"),
                    "label": model.get("name"),
                }
                for model in (models or {}).get("models") or []
            ],
            "note": "An agent's model is {provider, name} from these; both "
            "empty runs on the default.",
        }
    except ApiRefused as refused:
        found["models"] = {"unavailable": refused.reason}
    return found


def listed(blocks: Mapping[str, Any], section: str) -> list[dict[str, Any]]:
    """:return: One of :func:`building_blocks`' lists; empty when it couldn't be read."""
    found = blocks.get(section)
    return found if isinstance(found, list) else []


def known(blocks: Mapping[str, Any], section: str) -> dict[str, dict[str, Any]] | None:
    """:return: A list's records by ID; None when it couldn't be read, so
    nothing is said to be gone."""
    found = blocks.get(section)
    if not isinstance(found, list):
        return None
    return {str(record.get("id")): record for record in found}


# ---------------------------------------------------------------- shaping


def start_fields(document: Mapping[str, Any]) -> dict[str, Any]:
    """:return: A workflow's input fields: its start's input_schema."""
    start = next(
        (
            node
            for node in document.get("nodes") or []
            if isinstance(node, dict) and node.get("kind") == "start"
        ),
        None,
    )
    schema = ((start or {}).get("config") or {}).get("input_schema")
    return schema if isinstance(schema, dict) else {}


def ui_component_block(record: Mapping[str, Any]) -> dict[str, Any]:
    """A team UI component, as a Rich answers node picks it: by its key."""
    capabilities = record.get("capabilities") or {}
    return {
        "key": record.get("key"),
        "name": record.get("display_name"),
        "description": record.get("description") or "",
        "inputs": sorted((record.get("input_schema") or {}).get("properties") or {}),
        "has_choices": bool(capabilities.get("has_choices")),
        "has_actions": bool(capabilities.get("has_actions")),
        "note": "Pick it by key in a Rich answers node's components; the model "
        "fills its inputs and the layout is the component's.",
    }


def workflow_block(record: Mapping[str, Any]) -> dict[str, Any]:
    document = record.get("document") or {}
    return {
        "id": record.get("id"),
        "name": document.get("name"),
        "description": document.get("description") or "",
        "status": record.get("status"),
        "published_version": record.get("published_version"),
        "input_fields": start_fields(document),
    }


def agent_block(record: Mapping[str, Any]) -> dict[str, Any]:
    document = record.get("document") or {}
    nodes = [node for node in document.get("nodes") or [] if isinstance(node, dict)]
    entry = next((node for node in nodes if node.get("kind") == "agent"), {})
    # What its chat takes: the state its chat agent declares.
    state = next(
        (
            (node.get("config") or {}).get("state_schema")
            for node in nodes
            if isinstance((node.get("config") or {}).get("state_schema"), dict)
        ),
        None,
    )
    found = {
        "id": record.get("id"),
        "name": document.get("name"),
        "description": document.get("description")
        or (entry.get("config") or {}).get("description")
        or "",
        "status": record.get("status"),
        "published_version": record.get("published_version"),
    }
    if isinstance(state, dict) and state.get("properties"):
        found["inputs"] = state
    return found


def credential_block(record: Mapping[str, Any]) -> dict[str, Any]:
    auth = record.get("auth") or {}
    return {
        **pick(record, ("id", "name", "description", "urls")),
        "auth": auth.get("method") or auth.get("kind"),
    }


def knowledge_block(record: Mapping[str, Any]) -> dict[str, Any]:
    found = pick(record, ("id", "name", "description", "kind"))
    if record.get("kind") == "system":
        found["ingested_repositories"] = record.get("ingested", 0)
    else:
        found["searchable_documents"] = record.get("ready", 0)
    return found


def mcp_block(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **pick(record, ("id", "name", "description", "status")),
        "tools": [
            tool.get("name")
            for tool in record.get("tools") or []
            if isinstance(tool, dict)
        ],
    }
