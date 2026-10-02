"""
The settings of a ``forge.agent/v1`` document's Forge step kinds (the start,
approval, http, transform, delay, if, switch, match, loop, merge and end), as
their factories read them (``graph.factories.base.settings_of``). Keys a kind
doesn't know are ignored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EntryConfig(_Config):
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ApprovalConfig(_Config):
    message: str = ""
    approvers: Literal["org:admin", "org:member"] = "org:admin"
    timeout_hours: float = 0


class Header(_Config):
    id: str = ""
    name: str = ""
    value: str = ""


class HttpConfig(_Config):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = ""
    headers: list[Header] = Field(default_factory=list)
    body: str = ""
    timeout_seconds: float = 30
    retries: int = 0
    # A JSON Schema of the response's body; empty leaves it undeclared.
    output_schema: dict[str, Any] = Field(default_factory=dict)


class TransformConfig(_Config):
    expression: str = ""
    output_schema: dict[str, Any] = Field(default_factory=dict)


#: A delay's unit, in seconds.
UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


class DelayConfig(_Config):
    amount: float = 0
    unit: Literal["seconds", "minutes", "hours", "days"] = "minutes"


class IfConfig(_Config):
    condition: str = ""


class SwitchCase(_Config):
    id: str
    value: str = ""


class SwitchConfig(_Config):
    value: str = ""
    cases: list[SwitchCase] = Field(default_factory=list)


class MatchArm(_Config):
    id: str
    label: str = ""
    condition: str = ""


class MatchConfig(_Config):
    arms: list[MatchArm] = Field(default_factory=list)


class LoopConfig(_Config):
    items: str = ""
    item_name: str = "item"
    max_iterations: int = 100
    concurrency: int = 1


class MergeConfig(_Config):
    mode: Literal["all", "any"] = "all"


class EndConfig(_Config):
    outcome: Literal["succeeded", "failed"] = "succeeded"
    result: str = ""
