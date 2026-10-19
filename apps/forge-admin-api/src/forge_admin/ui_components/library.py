"""
The organization's team UI components as the hosted runtime builds agents
with them: their latest published, enabled versions, as they are now
(:class:`OrganizationUiComponents`), and a stamp of those versions in each
agent's runner cache key (:class:`UiComponentStamps`), so a publish, a
disable or an archive reaches the agents that pick the component from their
next turn, on every process, and no other agent's runner is built again.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from forge_agent_runtime.generative_ui import UiComponentDefinition, team_keys

from forge_admin.ui_components.store import UiComponentStore


class OrganizationUiComponents:
    """
    The team components an agent's Rich answers nodes pick, by key
    (``forge_agent_runtime.generative_ui.UiComponentLibrary``): its
    organization's latest published, enabled versions.
    """

    def __init__(self, store: UiComponentStore) -> None:
        self.store = store

    async def __call__(
        self, keys: Sequence[str], *, organization_id: str | None
    ) -> list[UiComponentDefinition]:
        """
        :raises LookupError: One isn't the organization's to use.
        :raises PyMongoError: The store isn't answering: the build fails
            rather than going on without it.
        """
        if organization_id is None:
            raise LookupError("The agent belongs to no organization.")
        found = await self.store.find_live(organization_id, keys)
        missing = [key for key in keys if key not in found]
        if missing:
            raise LookupError(
                f"the organization has no team component {missing[0]} to use: it "
                "was archived or disabled, or isn't published yet; pick another"
            )
        return [UiComponentDefinition.model_validate(found[key]) for key in keys]


class UiComponentStamps:
    """
    When the team components an agent picks last changed as agents use them
    (published versions, disabled or not, archived), for the hosted runtime's
    cache: an agent built before one of them changed is built again.
    """

    def __init__(self, store: UiComponentStore) -> None:
        self.store = store

    async def __call__(
        self, organization_id: str | None, document: Mapping[str, Any]
    ) -> str:
        """:return: Empty for an agent that picks none; otherwise their versions' digest."""
        keys = team_keys(document)
        if not keys or organization_id is None:
            return ""
        found = await self.store.stamps(organization_id, keys)
        # Published versions only go up, so a stamp never comes back to an earlier one.
        state = [[key, *found.get(key, (None, None))] for key in sorted(keys)]
        return hashlib.sha256(json.dumps(state).encode()).hexdigest()[:16]
