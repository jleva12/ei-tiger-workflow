"""A chat agent with what it needs to run outside Forge: the saved agents it
uses (their latest published versions) bundled into its ``dependencies``, and
notes on what only Forge's runtime has. The export and the standalone project
(the starter) are both made of it.
"""

from typing import Any

from forge_admin.chat_agents.store import ChatAgentStore

#: Nodes a runtime outside Forge can't run without a service of its own, and what each does.
HOSTED_ONLY = {
    "adk_workflow": "runs a workflow, which only Forge's runtime can",
    "knowledge_base": "searches the organization's knowledge bases, which only Forge's runtime can",
}


async def bundle(
    store: ChatAgentStore,
    organization_id: str,
    document: dict[str, Any],
    *,
    hosted: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """
    :param store: The organization's chat agents.
    :param organization_id: The organization.
    :param document: An agent's document (a version's, or its draft).
    :param hosted: Note what runs only in Forge; the starter notes it itself,
        with the service a project can give it.
    :return: The document with its saved agents in ``dependencies``, and the
        notes on what runs only in Forge or can't be bundled.
    :raises PyMongoError: The store isn't answering.
    """
    document = dict(document)
    dependencies: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    pending = [document]
    while pending:
        current = pending.pop()
        for node in current.get("nodes") or []:
            kind, config = node.get("kind"), node.get("config") or {}
            name = node.get("name")
            if kind in HOSTED_ONLY:
                if hosted:
                    notes.append(f"{name} {HOSTED_ONLY[kind]}")
            elif kind == "mcp" and config.get("server"):
                if hosted:
                    notes.append(f"{name} uses one of the organization's MCP servers")
            elif kind == "saved_agent" and config.get("agent"):
                used = await store.get(organization_id, str(config["agent"]))
                latest = used.get("published_version") if used else None
                if latest is None:
                    notes.append(f"{name} uses an agent that isn't published")
                    continue
                key = f"{config['agent']}@{latest}"
                if key in dependencies:
                    continue
                bundled = await store.version(
                    organization_id, str(config["agent"]), int(latest)
                )
                if bundled is not None:
                    dependencies[key] = bundled["document"]
                    pending.append(bundled["document"])
    if dependencies:
        document["dependencies"] = dependencies
    return document, notes
