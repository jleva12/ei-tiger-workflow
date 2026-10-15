"""The node catalog: the builder's kinds, kept in step with the factories that build them."""

import json
from importlib import resources
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from forge_task_adk_workflows.graph import FACTORIES, NODE_CATALOG, NodeCatalog, outputs_of
from forge_task_adk_workflows.support.step_settings import (
    ApprovalConfig,
    BashConfig,
    DecisionBoardConfig,
    DelayConfig,
    EndConfig,
    EntryConfig,
    FilterConfig,
    GitBranchConfig,
    GitCloneConfig,
    GithubPullRequestConfig,
    GitPushConfig,
    HttpConfig,
    IfConfig,
    LoopConfig,
    MatchConfig,
    MergeConfig,
    PiAgentConfig,
    SwitchConfig,
    TransformConfig,
)

# The kinds whose factories read their settings with a model.
MODELS: dict[str, type[BaseModel]] = {
    "start": EntryConfig,
    "approval": ApprovalConfig,
    "decision_board": DecisionBoardConfig,
    "http": HttpConfig,
    "bash": BashConfig,
    "git_clone": GitCloneConfig,
    "git_branch": GitBranchConfig,
    "git_push": GitPushConfig,
    "github_pull_request": GithubPullRequestConfig,
    "transform": TransformConfig,
    "filter": FilterConfig,
    "delay": DelayConfig,
    "if": IfConfig,
    "switch": SwitchConfig,
    "match": MatchConfig,
    "loop": LoopConfig,
    "for_each": LoopConfig,
    "merge": MergeConfig,
    "end": EndConfig,
    "pi_agent": PiAgentConfig,
}


def raw() -> dict[str, Any]:
    text = resources.files("forge_task_adk_workflows.graph").joinpath("node_kinds.json").read_text("utf-8")
    return json.loads(text)


def test_every_kind_has_a_factory_and_every_factory_a_kind() -> None:
    assert [kind.kind for kind in NODE_CATALOG.kinds] == list(FACTORIES)


def test_every_group_has_a_kind() -> None:
    used = {kind.group for kind in NODE_CATALOG.kinds}
    assert [group.id for group in NODE_CATALOG.groups if group.id not in used] == []


@pytest.mark.parametrize("kind", sorted(MODELS))
def test_a_new_nodes_settings_are_its_kinds(kind: str) -> None:
    entry = NODE_CATALOG.find(kind)
    assert entry is not None
    MODELS[kind].model_validate(entry.defaults)


def test_a_new_nodes_ways_out_are_the_ones_its_graph_has() -> None:
    for entry in NODE_CATALOG.kinds:
        node = {"id": "n", "kind": entry.kind, "config": entry.defaults}
        rows = [row["id"] for row in entry.defaults[entry.rows.setting]] if entry.rows else []
        if entry.rows and entry.rows.ways:
            rows = [f"{row}:{way.id}" for row in rows for way in entry.rows.ways]
        assert outputs_of(node) == [*rows, *(output.id for output in entry.outputs)], entry.kind


def test_ways_out_by_kind() -> None:
    def ways(kind: str, config: dict[str, Any] | None = None) -> list[str]:
        return outputs_of({"id": "n", "kind": kind, "config": config or {}})

    assert ways("end") == []
    assert ways("llm") == ["next"]
    assert ways("http") == ["success", "error"]
    assert ways("bash") == ["success", "error"]
    assert ways("approval") == ["approved", "rejected"]
    assert ways("if") == ["true", "false"]
    assert ways("loop") == ["each", "done"]
    assert ways("for_each") == ["each", "done"]
    # A row without an ID, or that isn't a row, is no way out.
    assert ways("switch", {"cases": [{"id": "a"}, {"value": "x"}, "b", {"id": "c"}]}) == ["a", "c", "default"]
    assert ways("match", {"arms": [{"id": "first"}]}) == ["first", "otherwise"]
    assert ways("match", {"arms": None}) == ["otherwise"]
    # A board's choice has two ways, before Done.
    assert ways("decision_board", {"choices": [{"id": "ship"}, {"label": "x"}, {"id": "hold"}]}) == [
        "ship:accepted",
        "ship:rejected",
        "hold:accepted",
        "hold:rejected",
        "done",
    ]
    # A kind it doesn't know goes on by next, as before (building refuses it).
    assert ways("nope") == ["next"]


def test_rows_are_numbered_from_one_when_blank() -> None:
    match = NODE_CATALOG.find("match")
    assert match is not None and match.rows is not None
    assert match.rows.empty.format(n=2) == "Rule 2"


def test_a_kind_in_a_group_it_doesnt_have_is_refused() -> None:
    data = raw()
    data["kinds"][0]["group"] = "elsewhere"
    with pytest.raises(ValidationError, match="isn't one of the groups"):
        NodeCatalog.model_validate(data)


def test_a_kind_twice_is_refused() -> None:
    data = raw()
    data["kinds"].append(data["kinds"][0])
    with pytest.raises(ValidationError, match="a kind is listed twice"):
        NodeCatalog.model_validate(data)


def test_a_way_out_twice_is_refused() -> None:
    data = raw()
    data["kinds"][0]["outputs"] = [{"id": "next", "label": "Next"}, {"id": "next", "label": "Again"}]
    with pytest.raises(ValidationError, match="has a way out twice"):
        NodeCatalog.model_validate(data)


def test_a_setting_it_doesnt_know_is_refused() -> None:
    data = raw()
    data["kinds"][0]["colour"] = "red"
    with pytest.raises(ValidationError):
        NodeCatalog.model_validate(data)
