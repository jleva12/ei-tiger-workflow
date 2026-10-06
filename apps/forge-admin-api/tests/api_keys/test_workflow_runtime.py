"""The workflows for outside apps (``/runtime/workflows``): running one by
reference, following the run, answering and deciding what it waits at, and
giving it up; who may do each, public and not."""

import asyncio
from typing import Any

import pytest
from key_world import ADMIN, MEMBER, NEIGHBOUR, VIEWER, World
from runtime_world import DRAFTY, INPUT, QUESTION, SHIP, THEIRS, Runtime

from forge_admin.adk_workflows.runs import settle


@pytest.fixture
def runtime(world: World) -> Runtime:
    return Runtime(world)


@pytest.fixture
def private(make_world: Any) -> Runtime:
    return Runtime(make_world(public=False))


# ------------------------------------------------------------ running


def test_a_workflow_says_what_it_takes(runtime: Runtime) -> None:
    info = runtime.send("GET", f"/{SHIP}").json()
    assert (info["ref"], info["version"], info["name"]) == (f"{SHIP}@1", 1, "Ship")
    assert info["input_schema"] == INPUT
    assert info["runs_url"].endswith(f"/runtime/workflows/{SHIP}/runs")
    assert info["a2a_card_url"].endswith(
        f"/runtime/a2a/{SHIP}/.well-known/agent-card.json"
    )
    assert runtime.send("GET", f"/{SHIP}@draft").json()["name"] == "Ship v2"


def test_anyone_runs_a_published_workflow_on_a_public_runtime(runtime: Runtime) -> None:
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
    assert (kept["requested_by"], kept["requested_by_name"]) == (
        "anonymous",
        "Anonymous caller",
    )
    assert kept["payload"]["trigger"] == {
        "type": "runtime",
        "protocol": "rest",
        "caller": None,
        "ref": f"{SHIP}@1",
    }
    # Its ID is how an anonymous caller follows it.
    assert runtime.send("GET", f"/{SHIP}/runs/{run['id']}").json()["id"] == run["id"]
    assert runtime.send("GET", f"/{DRAFTY}/runs/{run['id']}").status_code == 404


def test_an_unpublished_workflow_runs_only_as_its_draft(runtime: Runtime) -> None:
    refused = runtime.start(DRAFTY)
    assert refused.status_code == 404
    assert f"{DRAFTY}@draft" in refused.json()["detail"]
    assert runtime.start(f"{DRAFTY}@draft").json()["version"] == "draft"
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
    # Anonymous callers don't read a known caller's runs.
    assert runtime.send("GET", f"/{SHIP}/runs/{run['id']}").status_code == 404


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
            key="k",
            kind="approval",
            reason="Go?",
            details={},
            deadline=None,
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
    assert runtime.send("POST", path, decision).status_code == 404
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
    )
    assert refused.status_code == 403
    assert "never anonymously" in refused.json()["detail"]
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


# ------------------------------------------------------------ not public


def test_a_private_runtime_runs_for_those_with_agents_run_where_it_is(
    private: Runtime,
) -> None:
    assert private.start().status_code == 401
    assert private.send("GET", f"/{SHIP}").status_code == 401
    assert private.start(user=VIEWER).status_code == 403
    assert private.start(user=NEIGHBOUR).status_code == 403
    assert private.start(f"{THEIRS}", user=NEIGHBOUR).status_code == 201
    secret = private.world.make_key(name="CI")["secret"]
    assert private.start(key=secret).status_code == 201
    assert private.start(THEIRS, key=secret).status_code == 403
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
