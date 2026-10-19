"""The helper agents' configuration: ``helpers.yaml``, or the file
``FORGE_ADMIN_AGENT_HELPERS`` names.

Each helper is one entry: its name (what it's called by), its instruction,
the kit that reads its input and writes its result, its toolsets (as
``screens.yaml`` names them: a name, or a name and some of its tools), the
tool it hands its result in with, its model and thinking level, how long it
may take, what running it takes, and how it runs. :class:`.executor.Helpers`
checks the names in it against the kits and toolsets there are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from forge_admin.assistant.screens import ToolName, ToolsetRef

HELPERS_FILE = Path(__file__).with_name("helpers.yaml")

#: A helper's name: its ADK agent's and app's too, so an identifier.
HelperName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Permission = Annotated[str, StringConstraints(pattern=r"^[a-z_]+:[a-z_]+$")]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]


class HelperSpec(BaseModel):
    """One helper agent."""

    model_config = ConfigDict(extra="forbid")

    name: HelperName
    #: What the person calls it, e.g. "Instruction writer".
    title: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    #: What it does; its ADK agent's description.
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    #: How messages name it: "the writer" in "The writer took too long".
    called: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    #: Its kit (``registry.KITS``); its name when omitted.
    kit: HelperName | None = None
    #: ``sync``: the request waits for its result. ``background``: the
    #: request gets a job, and the result is on it when it's done.
    run: Literal["sync", "background"] = "sync"
    #: What running it, and dismissing its jobs, takes in the organization.
    permission: Permission
    #: How long it may take.
    seconds: float = Field(default=120.0, gt=0, le=900)
    #: One of the assistant's models; its default when omitted.
    model: str | None = None
    #: The nearest the model offers is used; the provider's default when omitted.
    thinking_level: ThinkingLevel | None = None
    toolsets: list[ToolsetRef] = Field(default_factory=list)
    #: The tool it finishes by calling: the last call that tool accepted is
    #: its result. Without one, its last answer is.
    hand_in: ToolName | None = None
    #: Its system instruction, as written: ``{{ }}`` and braces are plain text.
    instruction: Annotated[str, StringConstraints(min_length=1)]

    @property
    def kit_name(self) -> str:
        return self.kit or self.name

    @property
    def subject(self) -> str:
        """``called``, to start a sentence with: "The writer"."""
        return self.called[:1].upper() + self.called[1:]


class HelperConfig(BaseModel):
    """The whole configuration: ``helpers.yaml``."""

    model_config = ConfigDict(extra="forbid")

    helpers: list[HelperSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> HelperConfig:
        names = [helper.name for helper in self.helpers]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"helper names must be unique: {', '.join(repeated)}")
        return self

    @classmethod
    def load(cls, path: Path = HELPERS_FILE) -> HelperConfig:
        """
        :param path: A YAML file in this shape.
        :raises ValueError: It isn't valid YAML, or not this shape.
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(data)
        except (yaml.YAMLError, ValidationError) as error:
            raise ValueError(
                f"{path} isn't a valid helper configuration: {error}"
            ) from None
