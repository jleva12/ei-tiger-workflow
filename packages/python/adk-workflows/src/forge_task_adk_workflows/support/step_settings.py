"""
The settings of a ``forge.agent/v1`` document's Forge step kinds (the start,
approval, http, transform, filter, delay, if, switch, match, loop, for_each,
merge and end), as their factories read them
(``graph.factories.base.settings_of``). Keys a kind doesn't know are ignored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EntryConfig(_Config):
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # Whether a run may start with files, and of which types (ids of
    # forge_task_adk_workflows.files.FILE_TYPES); none takes any type.
    allow_files: bool = False
    file_types: list[str] = Field(default_factory=list)
    # Whether a run ADK can't carry on where it stopped starts over in a new
    # session, its steps running again (runs.AdkRun._recover); off, it fails.
    start_over: bool = False


class ApprovalConfig(_Config):
    message: str = ""
    approvers: Literal["org:admin", "org:member"] = "org:admin"
    timeout_hours: float = 0


class Header(_Config):
    """A header, or a query parameter: its name and its value, templates."""

    id: str = ""
    name: str = ""
    value: str = ""


class HttpConfig(_Config):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = ""
    # Query parameters, added to the URL's own, each name and value encoded.
    query: list[Header] = Field(default_factory=list)
    headers: list[Header] = Field(default_factory=list)
    body: str = ""
    timeout_seconds: float = 30
    retries: int = 0
    # A JSON Schema of the response's body; empty leaves it undeclared.
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # One of the organization's credentials, by ID: signs its requests, sent
    # only to the URLs it's for. Empty for none.
    credential: str = ""


class TransformConfig(_Config):
    expression: str = ""
    output_schema: dict[str, Any] = Field(default_factory=dict)


class FilterConfig(_Config):
    items: str = ""
    # What its condition calls the item it's on.
    item_name: str = "item"
    condition: str = ""


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
    # "first" takes the first rule that holds; "all" takes every one, at once.
    mode: Literal["first", "all"] = "first"


class LoopConfig(_Config):
    # A loop's, and a for-each's.
    items: str = ""
    item_name: str = "item"
    max_iterations: int = 100
    concurrency: int = 1


class MergeConfig(_Config):
    mode: Literal["all", "any"] = "all"


class EndConfig(_Config):
    outcome: Literal["succeeded", "failed"] = "succeeded"
    result: str = ""
