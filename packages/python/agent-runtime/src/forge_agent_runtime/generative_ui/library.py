"""Where an agent's team UI components come from as it's built.

A Rich answers node picks base components by name and team components by
key (``org.…``). A document carries the team components it (and its saved
agents) pick in its ``ui_components``, by key, for running it elsewhere: an
export, the standalone starter, a workflow run. The hosted runtime finds the
rest through :class:`UiComponentLibrary` (``RuntimeServices.ui_components``),
at their latest published versions, as it finds the organization's skills.
Building never falls back to base components alone: a team component it
can't find is a build error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from forge_agent_runtime.generative_ui.definitions import PREFIX, UiComponentDefinition


class UiComponentLibrary(Protocol):
    """Finds the organization's team components a Rich answers node picks, by key."""

    async def __call__(
        self, keys: Sequence[str], *, organization_id: str | None
    ) -> list[UiComponentDefinition]:
        """
        :return: Their latest published versions, in order.
        :raises LookupError: The organization has no such component published
            and enabled (archived, disabled, never published, or none).
        """
        ...


def is_team_key(name: Any) -> bool:
    """:return: Whether a component name is a team component's key."""
    return isinstance(name, str) and name.startswith(PREFIX)


def team_keys(document: Mapping[str, Any]) -> list[str]:
    """:return: The team components a chat agent's Rich answers nodes pick, by key, in order."""
    keys: list[str] = []
    for node in document.get("nodes") or []:
        if not isinstance(node, Mapping) or node.get("kind") != "rich_answers":
            continue
        config = node.get("config")
        for name in (config.get("components") if isinstance(config, Mapping) else None) or []:
            if is_team_key(name) and name not in keys:
                keys.append(name)
    return keys
