"""The workflows for outside apps (``/runtime/workflows``): running one by
reference, following the run, answering and deciding what it waits at, and
giving it up; who may do each: a key or a sign-in with agents:run where it
is, agents:manage for its draft."""

import asyncio
from typing import Any

import pytest
from agent_access_world import BUILDER, add_builder
from key_world import ADMIN, MEMBER, NEIGHBOUR, ORG, RUNTIME, VIEWER, World
from runtime_world import DRAFTY, INPUT, QUESTION, SHIP, THEIRS, Runtime

from forge_admin.adk_workflows.runs import settle


@pytest.fixture
def runtime(world: World) -> Runtime:
    return Runtime(world)


@pytest.fixture
def private(make_world: Any) -> Runtime:
    return Runtime(make_world())


# ------------------------------------------------------------ running


def test_a_workflow_says_what_it_takes(runtime: Runtime) -> None:
    info = runtime.send("GET", f"/{SHIP}").json()
    assert (info["ref"], info["version"], info["name"]) == (f"{SHIP}@1", 1, "Ship")
    assert info["input_schema"] == INPUT
    assert info["runs_url"].endswith(f"/runtime/workflows/{SHIP}/runs")
    assert info["a2a_card_url"].endswith(
        f"/runtime/a2a/{SHIP}/.well-known/agent-card.json"
    )
    # Its draft is for whoever edits the organization's agents.
    assert runtime.send("GET", f"/{SHIP}@draft").status_code == 403
    add_builder(runtime.world)
    assert (
        runtime.send("GET", f"/{SHIP}@draft", user=BUILDER).json()["name"] == "Ship v2"
    )


def test_the_runtime_needs_a_caller(runtime: Runtime) -> None:
    assert runtime.start(user=None).status_code == 401
    assert runtime.send("GET", f"/{SHIP}", user=None).status_code == 401
    assert runtime.call(runtime.runs.refused, ORG)[1] == 0


def test_a_member_runs_a_published_workflow(runtime: Runtime) -> None:
    started = runtime.start()
    assert started.status_code == 201, started.json()
    run = started.json()
    assert (run["status"], run["version"], run["workflow_name"]) == (
        "queued",
        1,
        "Ship",
    )
    assert started.headers["Location"] == run["links"]["self"]
    assert runtime.queue.queued == [run["id"]]
    kept = runtime.call(runtime.runs.get, run["id"])
    assert kept["requested_by"] == MEMBER
    assert kept["payload"]["trigger"]["ref"] == f"{SHIP}@1"
    assert runtime.send("GET", f"/{SHIP}/runs/{run['id']}").json()["id"] == run["id"]
    assert runtime.send("GET", f"/{DRAFTY}/runs/{run['id']}").status_code == 404


def test_an_unpublished_workflow_runs_only_as_its_draft(runtime: Runtime) -> None:
    refused = runtime.start(DRAFTY)
    assert refused.status_code == 404
    assert f"{DRAFTY}@draft" in refused.json()["detail"]
    # Run by whoever edits the organization's agents, not every member.
    assert runtime.start(f"{DRAFTY}@draft").status_code == 403
    add_builder(runtime.world)
    drafted = runtime.start(f"{DRAFTY}@draft", user=BUILDER)
    assert drafted.json()["version"] == "draft"
    assert runtime.start(f"{SHIP}@7").status_code == 404


def test_input_that_doesnt_fit_the_start_is_refused(runtime: Runtime) -> None:
    refused = runtime.send("POST", f"/{SHIP}/runs", {"input": {"nope": 1}})
    assert refused.status_code == 422
    assert "doesn't fit the start" in refused.json()["detail"]


def test_a_key_starts_runs_that_are_its_own(runtime: Runtime) -> None:
    secret = runtime.world.make_key(name="CI")["secret"]
    other = runtime.world.make_key(name="Other")["secret"]
    run = runtime.start(key=secret).json()
    kept = runtime.call(runtime.runs.get, run["id"])
    assert kept["requested_by"].startswith("apikey:")
    assert kept["requested_by_name"] == "API key CI"
    assert (
        runtime.send("GET", f"/{SHIP}/runs/{run['id']}", key=secret).status_code == 200
    )
    # Another API caller key doesn't read the organization: not its run.
    assert (
        runtime.send("GET", f"/{SHIP}/runs/{run['id']}", key=other).status_code == 404
    )
    # A viewer does, and a member.
    assert (
        runtime.send("GET", f"/{SHIP}/runs/{run['id']}", user=VIEWER).status_code == 200
    )
    # Nor does anyone unknown.
    assert (
        runtime.send("GET", f"/{SHIP}/runs/{run['id']}", user=None).status_code == 401
    )
    # Nor anyone outside its organization: there's no such run.
    assert (
        runtime.send("GET", f"/{SHIP}/runs/{run['id']}", user=NEIGHBOUR).status_code
        == 404
    )


def test_a_start_retried_with_its_idempotency_key_is_the_run_it_made(
    runtime: Runtime,
) -> None:
    secret = runtime.world.make_key(name="CI")["secret"]

    def start(key: str, value: str = "a1", *, by: str = secret) -> Any:
        return runtime.world.call(
            "POST",
            f"/workflows/{SHIP}/runs",
            None,
            {"input": {"key": value}},
            key=by,
            prefix=RUNTIME,
            headers={"Idempotency-Key": key},
        )

    first = start("order-17")
    assert first.status_code == 201, first.json()
    again = start("order-17")
    # The same run, answered as it is: no second run, no second job.
    assert again.status_code == 200, again.json()
    assert again.json()["id"] == first.json()["id"]
    assert runtime.queue.queued == [first.json()["id"]]
    # Another key is another start.
    other = start("order-18")
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]
    # The key with another input: refused, not the run, not a new one.
    assert start("order-17", "b2").status_code == 409
    # Another caller's key of the same name is theirs: not this run.
    neighbour = runtime.world.make_key(name="Other")["secret"]
    theirs = start("order-17", by=neighbour)
    assert theirs.status_code == 409
    assert len(runtime.queue.queued) == 2


def test_anonymous_callers_cant_start_runs_with_idempotency_keys(
    runtime: Runtime,
) -> None:
    # The runtime has no anonymous callers, so a key never names an anonymous run.
    started = runtime.world.call(
        "POST",
        f"/workflows/{SHIP}/runs",
        None,
        {"input": {"key": "a1"}},
        prefix=RUNTIME,
        headers={"Idempotency-Key": "k-1"},
    )
    assert started.status_code == 401


def test_waiting_is_capped_and_comes_back_with_the_run(make_world: Any) -> None:
    runtime = Runtime(make_world(workflow_runtime_wait=0.3))
    run = runtime.start(wait=30).json()
    # Nothing takes it here: the wait runs out at the server's limit.
    assert run["status"] == "queued"
    request_id = runtime.paused(run["id"], "human_input", response_schema=QUESTION)
    paused = runtime.send("GET", f"/{SHIP}/runs/{run['id']}?wait=30").json()
    assert paused["status"] == "paused"
    assert paused["pause"]["id"] == request_id
    assert paused["pause"]["response_schema"] == QUESTION
    assert paused["links"]["answers"].endswith(f"/runs/{run['id']}/answers")


def test_settle_stops_at_a_pause(runtime: Runtime) -> None:
    run = runtime.start().json()

    async def pause_soon() -> None:
        await asyncio.sleep(0.3)
        await runtime.runs.claim(run["id"], owner="w1", lease_seconds=60)
        await runtime.runs.pause(
            run["id"],
            owner="w1",
            state={},
            gates=[
                {
                    "key": "k",
                    "kind": "approval",
                    "reason": "Go?",
                    "details": {},
                    "deadline": None,
                }
            ],
        )

    async def follow() -> Any:
        task = asyncio.create_task(pause_soon())
        settled = await settle(runtime.runs, run["id"], wait=5)
        await task
        return settled

    assert runtime.call(follow)["status"] == "paused"


# ------------------------------------------------------------ pauses


def test_the_starter_answers_its_runs_question(runtime: Runtime) -> None:
    run = runtime.start().json()
    request_id = runtime.paused(run["id"], "human_input", response_schema=QUESTION)
    path = f"/{SHIP}/runs/{run['id']}/answers"
    bad = runtime.send(
        "POST", path, {"request_id": request_id, "answer": {"send": "yes"}}
    )
    assert bad.status_code == 422
    wrong = runtime.send("POST", path, {"request_id": "nope", "answer": {"send": True}})
    assert wrong.status_code == 409
    answered = runtime.send(
        "POST", path, {"request_id": request_id, "answer": {"send": True}}
    )
    assert answered.status_code == 200, answered.json()
    assert answered.json()["status"] == "queued"
    assert runtime.queue.queued[-1] == run["id"]


def test_approvals_are_decided_by_those_the_step_names(runtime: Runtime) -> None:
    secret = runtime.world.make_key(name="CI")["secret"]
    run = runtime.start(key=secret).json()
    request_id = runtime.paused(run["id"], "approval", approvers="org:admin")
    path = f"/{SHIP}/runs/{run['id']}/decisions"
    decision = {"request_id": request_id, "approved": True, "comment": "Fine"}
    # A key that ran it doesn't decide the admins' approval.
    refused = runtime.send("POST", path, decision, key=secret)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Requires agents:approve"
    assert runtime.send("POST", path, decision, user=None).status_code == 401
    decided = runtime.send("POST", path, decision, user=ADMIN)
    assert decided.status_code == 200, decided.json()
    assert decided.json()["status"] == "queued"


def test_anonymous_callers_never_decide(runtime: Runtime) -> None:
    run = runtime.start().json()
    request_id = runtime.paused(
        run["id"],
        "approval",
        approvers="org:member",
        confirmation=True,
        tool="refund",
        args={"amount": 5},
    )
    pause = runtime.send("GET", f"/{SHIP}/runs/{run['id']}").json()["pause"]
    assert (pause["confirmation"], pause["tool"], pause["args"]) == (
        True,
        "refund",
        {"amount": 5},
    )
    refused = runtime.send(
        "POST",
        f"/{SHIP}/runs/{run['id']}/decisions",
        {"request_id": request_id, "approved": True},
        user=None,
    )
    assert refused.status_code == 401
    # A member confirms a tool call.
    confirmed = runtime.send(
        "POST",
        f"/{SHIP}/runs/{run['id']}/decisions",
        {"request_id": request_id, "approved": False},
        user=MEMBER,
    )
    assert confirmed.status_code == 200


def test_the_starter_gives_its_run_up(runtime: Runtime) -> None:
    run = runtime.start().json()
    cancelled = runtime.send("POST", f"/{SHIP}/runs/{run['id']}/cancel").json()
    assert cancelled["status"] == "abandoned"
    assert "cancel" not in cancelled["links"]


def test_a_running_run_is_stopped_by_its_worker_then_given_up(
    runtime: Runtime,
) -> None:
    run = runtime.start().json()
    assert runtime.call(runtime.runs.claim, run["id"], owner="w1", lease_seconds=60)
    going = runtime.send("GET", f"/{SHIP}/runs/{run['id']}").json()
    assert "cancel" in going["links"] and going["cancel_requested_at"] is None

    asked = runtime.send("POST", f"/{SHIP}/runs/{run['id']}/cancel")

    assert asked.status_code == 202, asked.json()
    stopping = asked.json()
    assert stopping["status"] == "running" and stopping["cancel_requested_at"]
    assert "cancel" not in stopping["links"]
    runtime.call(runtime.runs.abandon_running, run["id"], owner="w1", state={})
    given_up = runtime.send("GET", f"/{SHIP}/runs/{run['id']}").json()
    assert given_up["status"] == "abandoned"


# ------------------------------------------------------------ who may


def test_a_private_runtime_runs_for_those_with_agents_run_where_it_is(
    private: Runtime,
) -> None:
    assert private.start(user=None).status_code == 401
    assert private.send("GET", f"/{SHIP}", user=None).status_code == 401
    assert private.start(user=VIEWER).status_code == 403
    # Another organization's workflow is one there isn't.
    assert private.start(user=NEIGHBOUR).status_code == 404
    assert private.start(f"{THEIRS}", user=NEIGHBOUR).status_code == 201
    secret = private.world.make_key(name="CI")["secret"]
    assert private.start(key=secret).status_code == 201
    assert private.start(THEIRS, key=secret, user=None).status_code == 404
    member_run = private.start(user=MEMBER).json()
    # Even its organization's API caller key can't read a member's run.
    assert (
        private.send("GET", f"/{SHIP}/runs/{member_run['id']}", key=secret).status_code
        == 404
    )
    assert (
        private.send("GET", f"/{SHIP}/runs/{member_run['id']}", user=ADMIN).status_code
        == 200
    )


# ------------------------------------------------------------ provenance


def started(runtime: Runtime, run: Any) -> dict[str, Any]:
    """How the run was started, as the store keeps it."""
    assert run.status_code == 201, run.json()
    kept = runtime.call(runtime.runs.get, run.json()["id"])
    return {
        key: kept[key]
        for key in (
            "trigger_type",
            "channel",
            "on_behalf_of",
            "on_behalf_of_name",
            "on_behalf_of_verified",
            "api_key_id",
            "auth_method",
            "client_ip",
        )
    }


def test_a_signed_in_callers_run_is_theirs_verified(runtime: Runtime) -> None:
    found = started(runtime, runtime.start(user=MEMBER))
    assert (found["on_behalf_of"], found["on_behalf_of_verified"]) == (MEMBER, True)
    assert found["auth_method"] == "jwt" and found["api_key_id"] is None


def test_a_keys_run_is_the_keys_with_it(runtime: Runtime) -> None:
    key = runtime.world.make_key(name="CI")
    found = started(runtime, runtime.start(key=key["secret"]))
    assert found["on_behalf_of"] == f"apikey:{key['id']}"
    assert (found["on_behalf_of_name"], found["on_behalf_of_verified"]) == (
        "API key CI",
        True,
    )
    assert (found["api_key_id"], found["auth_method"]) == (key["id"], "api_key")
    # On the admin API's run route, a key's start is an API key's.
    admin = runtime.world.call(
        "POST",
        f"/organizations/{ORG}/agents/{SHIP}/runs",
        None,
        {"input": {"key": "a1"}},
        key=key["secret"],
    )
    assert admin.status_code == 202, admin.json()
    assert admin.json()["provenance"] == {
        "trigger_type": "api_key",
        "channel": "api",
        "on_behalf_of": {
            "id": f"apikey:{key['id']}",
            "name": "API key CI",
            "verified": True,
        },
        "via": None,
        "api_key_id": key["id"],
        "auth_method": "api_key",
    }


def test_a_start_the_runtime_refuses_is_kept(
    runtime: Runtime, private: Runtime
) -> None:
    misfit = runtime.send("POST", f"/{SHIP}/runs", {"input": {"nope": 1}})
    assert misfit.status_code == 422
    [refused], _ = runtime.call(runtime.runs.refused, ORG)
    assert (refused["agent_id"], refused["status_code"], refused["version"]) == (
        SHIP,
        422,
        "1",
    )
    assert (refused["trigger_type"], refused["on_behalf_of_verified"]) == (
        "runtime_rest",
        True,
    )
    # Someone of the organization without agents:run is refused, and it's kept.
    assert private.start(user=VIEWER).status_code == 403
    [kept], _ = private.call(private.runs.refused, ORG)
    assert (kept["status_code"], kept["on_behalf_of"], kept["auth_method"]) == (
        403,
        VIEWER,
        "jwt",
    )
    # Reading a workflow isn't starting it: nothing more is kept.
    assert private.send("GET", f"/{SHIP}", user=VIEWER).status_code == 403
    assert private.call(private.runs.refused, ORG)[1] == 1
