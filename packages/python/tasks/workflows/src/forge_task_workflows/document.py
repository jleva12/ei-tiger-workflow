"""The ``forge.workflow/v1`` document a run runs: steps (``nodes``) and the
connections between them (``edges``), as the web console's builder saves it
and the admin API keeps it. The admin checks every document against the
format's JSON Schema when it's saved; this reads one into typed settings and
the graph the engine walks.
"""

from __future__ import annotations

from collections import defaultdict
from functools import cached_property
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from forge_task_workflows.errors import WorkflowFailed

FORMAT = "forge.workflow/v1"


class _Config(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EntryConfig(_Config):
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ModelChoice(_Config):
    provider: str = ""
    name: str = ""


class AgentConfig(_Config):
    instructions: str = ""
    model: ModelChoice = Field(default_factory=ModelChoice)
    output: Literal["text", "json"] = "text"
    output_schema: str = ""
    # off, minimal, low, medium, high or xhigh: the nearest the model offers;
    # empty for the model's own.
    thinking_level: str = ""


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


class DelayConfig(_Config):
    amount: float = 0
    unit: Literal["seconds", "minutes", "hours", "days"] = "minutes"


class SubworkflowConfig(_Config):
    workflow: str = ""
    wait: bool = True


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


CONFIGS: dict[str, type[_Config]] = {
    "entry": EntryConfig,
    "agent": AgentConfig,
    "approval": ApprovalConfig,
    "http": HttpConfig,
    "transform": TransformConfig,
    "delay": DelayConfig,
    "subworkflow": SubworkflowConfig,
    "if": IfConfig,
    "switch": SwitchConfig,
    "match": MatchConfig,
    "loop": LoopConfig,
    "merge": MergeConfig,
    "end": EndConfig,
}

# Logic steps hand on what came into them: `previous` looks through them.
PASS_THROUGH = frozenset({"if", "switch", "match"})
# Steps that make a run wait (a person, a time, another system): a loop whose
# body has one runs its items one at a time.
WAITING = frozenset({"approval", "delay", "subworkflow"})
# A step's way out when it fails, if it has one.
ERROR_OUTPUT = {"http": "error"}
# Ways out a kind once had under another name, by kind: connections from
# them move (the web's document.ts moves them too). None at the moment.
RENAMED_OUTPUTS: dict[str, dict[str, str]] = {}


class Node(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)

    @cached_property
    def settings(self) -> Any:
        """The step's settings, typed by its kind."""
        model = CONFIGS.get(self.kind)
        if model is None:
            raise WorkflowFailed(f"Step {self.label} is a {self.kind!r}, which this worker doesn't run")
        try:
            return model.model_validate(self.config)
        except ValidationError as exc:
            raise WorkflowFailed(f"Step {self.label} has settings it can't run with: {exc}") from exc

    @property
    def label(self) -> str:
        return f"{self.name or self.id} ({self.id})"


class Edge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    source: str
    source_output: str = "next"
    target: str


class Workflow(BaseModel):
    """A workflow document, and its graph."""

    model_config = ConfigDict(extra="ignore")

    format: Annotated[str, Field(pattern=r"^forge\.workflow/v1$")] = FORMAT
    id: str = ""
    name: str = ""
    organization_id: str = ""
    entry: str | None = None
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    @classmethod
    def parse(cls, document: Any) -> Workflow:
        try:
            workflow = TypeAdapter(Workflow).validate_python(document)
        except ValidationError as exc:
            raise WorkflowFailed(f"It isn't a {FORMAT} document: {exc}") from exc
        if workflow.entry is None or workflow.entry not in workflow.by_id:
            raise WorkflowFailed("The workflow has no start step")
        for node in workflow.nodes:
            node.settings  # noqa: B018 - every step's settings read, before anything runs
        for edge in workflow.edges:
            source = workflow.by_id.get(edge.source)
            renamed = RENAMED_OUTPUTS.get(source.kind, {}) if source else {}
            edge.source_output = renamed.get(edge.source_output, edge.source_output)
        return workflow

    @cached_property
    def by_id(self) -> dict[str, Node]:
        return {node.id: node for node in self.nodes}

    @cached_property
    def _out(self) -> dict[tuple[str, str], list[str]]:
        out: dict[tuple[str, str], list[str]] = defaultdict(list)
        for edge in self.edges:
            if edge.source in self.by_id and edge.target in self.by_id:
                out[(edge.source, edge.source_output)].append(edge.target)
        return out

    def targets(self, node_id: str, output: str) -> list[str]:
        """The steps a way out of a step leads to, in the document's order."""
        return list(self._out.get((node_id, output), []))

    def sources(self, node_id: str) -> list[tuple[str, str]]:
        """The (step, way out) pairs that lead into a step."""
        return [(e.source, e.source_output) for e in self.edges if e.target == node_id and e.source in self.by_id]

    @cached_property
    def _reach(self) -> dict[str, frozenset[str]]:
        succ: dict[str, set[str]] = defaultdict(set)
        for (source, _), targets in self._out.items():
            succ[source].update(targets)
        reach: dict[str, frozenset[str]] = {}
        for start in self.by_id:
            seen: set[str] = set()
            stack = list(succ[start])
            while stack:
                at = stack.pop()
                if at in seen:
                    continue
                seen.add(at)
                stack.extend(succ[at])
            reach[start] = frozenset(seen)
        return reach

    def can_reach(self, source: str, target: str) -> bool:
        return target in self._reach.get(source, frozenset())

    def body_of(self, loop_id: str) -> frozenset[str]:
        """A loop's body: the steps its Each item way reaches before coming back to it."""
        seen: set[str] = set()
        stack = self.targets(loop_id, "each")
        while stack:
            at = stack.pop()
            if at == loop_id or at in seen:
                continue
            seen.add(at)
            for (source, _), targets in self._out.items():
                if source == at:
                    stack.extend(targets)
        return frozenset(seen)
