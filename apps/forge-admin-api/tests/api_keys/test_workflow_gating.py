"""Who may run a workflow limited to some of its organization's agent
permissions: those holding one, and those building its agents; nobody
unknown. Through the runtime (REST and A2A), the console, and as a saved
workflow another runs."""

from typing import Any

import pytest
from agent_access_world import (
    BUILDER,
    FINANCE,
    add_builder,
    limited_to,
    link,
    permission,
    with_groups,
)
from forge_agent_runtime.access import Caller, caller_json
from forge_task_adk_workflows.run_store import Actor
from key_world import ADMIN, MEMBER, NEIGHBOUR, ORG, RUNTIME, VIEWER, World
from runtime_world import DRAFTY, SHIP, THEIRS, Runtime, Workflows, workflow

from forge_admin.adk_workflows.resources import RunResources
from forge_admin.adk_workflows.runs import AdkRunRefused, prepare_run
from forge_admin.adk_workflows.versions import workflow_finder
from forge_admin.chat_agents.access import AgentUser


@pytest.fixture
def runtime(world: World) -> Runtime:
    found = Runtime(world)
    add_builder(world)
    return found


def workflows(runtime: Runtime) -> Workflows:
    found: Workflows = runtime.world.client.app.state.organization_agents  # type: ignore[attr-defined]
    return found


def limit(runtime: Runtime, workflow_id: str, *permission_ids: int) -> None:
    workflows(runtime).records[workflow_id]["access"] = limited_to(*permission_ids)


# ------------------------------------------------------------ the runtime


def test_a_limited_workflow_runs_only_for_those_holding_its_permission(
    runtime: Runtime,
) -> None:
    world = runtime.world
    shipping = permission(world, "shipping:run", [FINANCE])
    limit(runtime, SHIP, shipping)

    refused = runtime.start(user=MEMBER)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Ship v2 needs shipping:run in the organization"
    # Reading it is using it too.
    assert runtime.send("GET", f"/{SHIP}").status_code == 403

    # Through a linked company group.
    grouped = world.call(
        "POST",
        f"/workflows/{SHIP}/runs",
        None,
        {"input": {"key": "a1"}},
        prefix=RUNTIME,
        headers=with_groups(world, MEMBER, [FINANCE]),
    )
    assert grouped.status_code == 201, grouped.text
    # Those who build the organization's agents run them all.
    assert runtime.start(user=BUILDER).status_code == 201
    # Another organization's workflow is as it was, for its own.
    assert runtime.start(THEIRS, user=NEIGHBOUR).status_code == 201


def test_a_limited_workflows_a2a_card_isnt_for_everyone(runtime: Runtime) -> None:
    card = f"/a2a/{SHIP}/.well-known/agent-card.json"
    assert runtime.world.call("GET", card, MEMBER, prefix=RUNTIME).status_code == 200
    limit(runtime, SHIP, permission(runtime.world, "shipping:run"))
    refused = runtime.world.call("GET", card, MEMBER, prefix=RUNTIME)
    assert refused.status_code == 403
    assert runtime.world.call("GET", card, BUILDER, prefix=RUNTIME).status_code == 200


# ------------------------------------------------------------ the console


def test_the_console_runs_it_only_for_those_holding_its_permission(
    runtime: Runtime,
) -> None:
    world = runtime.world
    shipping = permission(world, "shipping:run")
    limit(runtime, SHIP, shipping)
    start = f"/organizations/{ORG}/agents/{SHIP}/runs"
    body = {"input": {"key": "a1"}, "version": 1}
    refused = world.call("POST", start, MEMBER, body)
    assert refused.status_code == 403
    assert "needs shipping:run" in refused.json()["detail"]
    link(world, shipping, groups=["Shippers"])
    shipper = with_groups(world, MEMBER, ["Shippers"])
    started = world.call("POST", start, None, body, headers=shipper)
    assert started.status_code == 202, started.text


# ------------------------------------------------------------ saved workflows


def test_a_workflow_running_a_limited_one_is_limited_too(runtime: Runtime) -> None:
    world = runtime.world
    shipping = permission(world, "shipping:run")
    limit(runtime, SHIP, shipping)
    store = workflows(runtime)
    # Drafty's agent calls Ship as a tool: its run runs Ship.
    caller = store.records[DRAFTY]
    caller["document"]["nodes"].append(
        {
            "id": "helper",
            "kind": "llm",
            "name": "Helper",
            "config": {
                "tools": [
                    {
                        "id": "t",
                        "kind": "adk_workflow",
                        "name": "Ship it",
                        "config": {"workflow": SHIP},
                    }
                ]
            },
        }
    )

    def resources(user: AgentUser | None) -> RunResources:
        return RunResources(
            ORG,
            chat_agents=None,
            sessions=world.sessions,
            user=user,
            workflows=store,  # type: ignore[arg-type]
        )

    async def start(user: AgentUser | None, record: dict[str, Any]) -> Any:
        return await prepare_run(
            record,
            {"wrong": "input"},
            workflow_finder(store, ORG),  # type: ignore[arg-type]
            resources(user),
        )

    # Refused before its input is checked: what it takes isn't theirs to know.
    with pytest.raises(AdkRunRefused, match="Ship v2 needs shipping:run"):
        world.run(lambda _: start(AgentUser(MEMBER), caller))
    with pytest.raises(AdkRunRefused, match="Ship v2 needs shipping:run"):
        world.run(lambda _: start(AgentUser(MEMBER), store.records[SHIP]))

    async def checked(user: AgentUser | None) -> None:
        await resources(user).check_workflows(
            caller, {f"{SHIP}@1": {"id": SHIP, "name": "Ship"}}
        )

    world.run(lambda _: checked(AgentUser(MEMBER, manager=True)))
    # Checking or publishing runs nothing, so checks nobody.
    world.run(lambda _: checked(None))
    link(world, shipping, groups=["Shippers"])
    world.run(lambda _: checked(AgentUser(MEMBER, ("Shippers",))))


# ------------------------------------------------------------ saying who may run it


def test_its_builders_say_who_may_run_a_workflow(runtime: Runtime) -> None:
    world = runtime.world
    shipping = permission(world, "shipping:run")
    workflows(runtime).records[SHIP].update(
        created_at="2026-10-15T00:00:00Z",
        created_by=ADMIN,
        updated_at="2026-10-15T00:00:00Z",
        updated_by=ADMIN,
        updated_by_name=None,
    )
    path = f"/organizations/{ORG}/agents/{SHIP}/access"
    assert (
        world.call("PUT", path, VIEWER, {"permission_ids": [shipping]}).status_code
        == 403
    )
    assert (
        world.call("PUT", path, BUILDER, {"permission_ids": [999]}).status_code == 422
    )
    made = world.call("PUT", path, BUILDER, {"permission_ids": [shipping]})
    assert made.status_code == 200, made.text
    assert made.json()["access"]["permission_ids"] == [shipping]
    assert runtime.start(user=MEMBER).status_code == 403
    world.call("PUT", path, BUILDER, {"permission_ids": []})
    assert runtime.start(user=MEMBER).status_code == 201


# ------------------------------------------------------------ resubmitting


def test_a_resubmission_holds_what_its_resubmitter_holds_now(runtime: Runtime) -> None:
    world = runtime.world
    refunds = permission(world, "refunds:approve", ["Approvers"])
    names = {str(refunds): "refunds:approve"}
    # A finished run whose agent only refunds:approve's holders may use, for
    # a member who held it as it started.
    desk = {
        "id": "ca_refunds",
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "name": "Refunds desk",
                "config": {"access": {"permission": refunds}},
            }
        ],
    }
    held = caller_json(
        Caller(held=frozenset({refunds}), names={refunds: "refunds:approve"})
    )
    run = runtime.call(
        runtime.runs.create,
        organization_id=ORG,
        agent_id=SHIP,
        agent_name="Ship",
        revision=4,
        session_id="session-1",
        payload={
            "tenant_id": ORG,
            "agent_id": SHIP,
            "document": workflow(SHIP, "Ship"),
            "chat_agents": {"ca_refunds": desk},
            "agent_access": held,
            "input": {"key": "a1"},
            "session_id": "session-1",
            "run_as": MEMBER,
        },
        requested_by=Actor(MEMBER, ""),
    )
    assert runtime.call(runtime.runs.claim, run["id"], owner="w1", lease_seconds=60)
    runtime.call(runtime.runs.finish, run["id"], owner="w1", state={}, succeeded=True)
    resubmit = f"/organizations/{ORG}/adk-runs/{run['id']}/resubmit"

    def payload_of(answer: Any) -> dict[str, Any]:
        assert answer.status_code == 202, answer.text
        again = runtime.call(runtime.runs.get, answer.json()["id"])
        return dict(again["payload"])

    # Their admin holds none of it: the resubmission holds nothing, not what
    # the run held; it still acts as the member.
    plain = payload_of(world.call("POST", resubmit, ADMIN))
    assert plain["agent_access"] == {
        "held": [],
        "names": names,
        "everything": False,
        "known": True,
    }
    assert plain["run_as"] == MEMBER
    # Signed in as an approver, they hold it.
    keyed = {**with_groups(world, ADMIN, ["Approvers"]), "Idempotency-Key": "again-1"}
    first = world.call("POST", resubmit, None, headers=keyed)
    assert payload_of(first)["agent_access"]["held"] == [refunds]
    # Sent again with its key, it's that resubmission, as it was made.
    retried = world.call(
        "POST", resubmit, ADMIN, headers={"Idempotency-Key": "again-1"}
    )
    assert retried.json()["id"] == first.json()["id"]
    assert payload_of(retried)["agent_access"]["held"] == [refunds]
