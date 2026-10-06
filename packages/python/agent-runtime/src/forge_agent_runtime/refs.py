"""How agents and workflows are named with their versions: ``ca_x`` (an
agent's latest published version), ``ca_x@3``, ``ca_x@draft``; and the same
for workflows, ``ag_x``, ``ag_x@3``, ``ag_x@draft``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Version = int | Literal["draft"] | None


class AgentNotFound(LookupError):
    """No agent (or no such version of one) by that name."""


@dataclass(frozen=True)
class AgentRef:
    """An agent and which of its versions: ``ca_x``, ``ca_x@3``, ``ca_x@draft``."""

    agent_id: str
    version: Version = None

    @classmethod
    def parse(cls, text: str) -> AgentRef:
        agent_id, _, version = text.partition("@")
        if not agent_id:
            raise AgentNotFound(f"{text!r} doesn't name an agent.")
        if not version:
            return cls(agent_id)
        if version == "draft":
            return cls(agent_id, "draft")
        if version.isdigit() and int(version) > 0:
            return cls(agent_id, int(version))
        raise AgentNotFound(f"{text!r}: a version is a number, or draft.")

    @classmethod
    def of(cls, agent_id: str, version: Any) -> AgentRef:
        """
        :param version: As a document's setting holds it: a number, "draft",
            or null (the latest published).
        """
        if version == "draft":
            return cls(agent_id, "draft")
        if isinstance(version, int) and not isinstance(version, bool) and version > 0:
            return cls(agent_id, version)
        return cls(agent_id)

    def __str__(self) -> str:
        return self.agent_id if self.version is None else f"{self.agent_id}@{self.version}"
