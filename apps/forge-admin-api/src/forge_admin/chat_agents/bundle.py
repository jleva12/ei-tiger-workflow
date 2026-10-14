"""A chat agent with what it needs to run outside Forge: the saved agents it
uses (their latest published versions) bundled into its ``dependencies``, the
skills its Skills tools (and theirs) use into its ``skills``, and notes on
what only Forge's runtime has. The export, the standalone project (the
starter) and a workflow run's agents are all made of it.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from forge_agent_runtime import SkillBundle

from forge_admin.chat_agents.store import ChatAgentStore
from forge_admin.skills.vault import skills_used

#: Finds the organization's skills by ID: those it has, whole.
FindSkills = Callable[[Sequence[str]], Awaitable[dict[str, SkillBundle]]]

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
    skills: FindSkills | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    :param store: The organization's chat agents.
    :param organization_id: The organization.
    :param document: An agent's document (a version's, or its draft).
    :param hosted: Note what runs only in Forge; the starter notes it itself,
        with the service a project can give it.
    :param skills: Finds the organization's skills, to bundle those its
        Skills tools use; None to leave them out (noted).
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
    used = [
        skill_id
        for current in (document, *dependencies.values())
        for skill_id in skills_used(current)
    ]
    used = list(dict.fromkeys(used))
    if used and skills is None:
        notes.append(
            "its Skills tools use the organization's skills, which aren't bundled"
        )
    elif used and skills is not None:
        found = await skills(used)
        if len(found) < len(used):
            notes.append("it uses a skill that was deleted")
        if found:
            document["skills"] = {
                skill_id: found[skill_id].model_dump(mode="json")
                for skill_id in used
                if skill_id in found
            }
    return document, notes
