"""The workflows over A2A: a workflow's card; a task starts a run, follows
it, asks for what it waits at and passes the answer on; reading the task
catches it up with its run; cancelling it gives the run up."""

import uuid
from typing import Any

import pytest
from key_world import ADMIN, AGENT, World
from runtime_world import INPUT, QUESTION, SHIP, Runtime

V1 = {"A2A-Version": "1.0"}


@pytest.fixture
def runtime(make_world: Any) -> Runtime:
    # Nothing takes runs here: tasks follow them only a moment.
    return Runtime(make_world(workflow_runtime_wait=0.2))


def rpc(
    runtime: Runtime,
    method: str,
    params: dict[str, Any],
    *,
    ref: str = SHIP,
    v1: bool = True,
    user: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    response = runtime.world.call(
        "POST",
        f"/a2a/{ref}",
        user,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        key=key,
        prefix="/api/v1/runtime",
        headers=V1 if v1 else None,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def send(runtime: Runtime, part: dict[str, Any], **task: Any) -> dict[str, Any]:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [part],
        **task,
    }
    return rpc(runtime, "SendMessage", {"message": message})


def get(runtime: Runtime, task_id: str) -> dict[str, Any]:
    return dict(rpc(runtime, "GetTask", {"id": task_id})["result"])


def run_of(runtime: Runtime, task: dict[str, Any]) -> dict[str, Any]:
    run = runtime.call(runtime.runs.get, uuid.UUID(task["id"]).hex)
    assert run is not None
    return dict(run)


def last_data(task: dict[str, Any]) -> dict[str, Any]:
    return dict(task["status"]["message"]["parts"][-1]["data"])


def test_a_workflows_card_says_what_it_takes(runtime: Runtime) -> None:
    card = runtime.world.call(
        "GET",
        f"/a2a/{SHIP}/.well-known/agent-card.json",
        None,
        prefix="/api/v1/runtime",
    ).json()
    assert (card["name"], card["version"]) == ("Ship", "1")
    [extension] = card["capabilities"]["extensions"]
    assert extension["params"]["input_schema"] == INPUT
    assert card["skills"][0]["tags"] == ["workflow"]
    assert {i["protocolVersion"] for i in card["supportedInterfaces"]} == {"1.0", "0.3"}
    # The chat agents are still there, beside them.
    chat = runtime.world.call(
        "GET",
        f"/a2a/{AGENT}/.well-known/agent-card.json",
        None,
        prefix="/api/v1/runtime",
    )
    assert chat.json()["name"] == "Acme helper"


def test_a_task_is_a_run_of_the_workflow(runtime: Runtime) -> None:
    task = send(runtime, {"data": {"key": "a1"}})["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_WORKING"
    run = run_of(runtime, task)
    assert (run["status"], run["version"], run["requested_by"]) == (
        "queued",
        "1",
        "anonymous",
    )
    assert run["payload"]["input"] == {"key": "a1"}
    assert run["payload"]["trigger"]["protocol"] == "a2a"
    assert run["payload"]["trigger"]["task_id"] == task["id"]
    assert task["metadata"]["forge"]["run_id"] == run["id"]


def test_input_comes_as_json_text_too_and_must_fit(runtime: Runtime) -> None:
    task = send(runtime, {"text": '{"key": "b2"}'})["result"]["task"]
    assert run_of(runtime, task)["payload"]["input"] == {"key": "b2"}
    rejected = send(runtime, {"text": "hello"})["result"]["task"]
    assert rejected["status"]["state"] == "TASK_STATE_REJECTED"
    assert "doesn't fit the start" in rejected["status"]["message"]["parts"][0]["text"]


def test_a_0_3_caller_sends_a_data_part(runtime: Runtime) -> None:
    got = rpc(
        runtime,
        "message/send",
        {
            "message": {
                "kind": "message",
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"key": "c3"}}],
            }
        },
        v1=False,
    )
    assert got["result"]["status"]["state"] == "working"


def test_a_question_is_asked_and_the_reply_answers_it(runtime: Runtime) -> None:
    task = send(runtime, {"data": {"key": "a1"}})["result"]["task"]
    run = run_of(runtime, task)
    request_id = runtime.paused(run["id"], "human_input", response_schema=QUESTION)
    # Reading the task catches it up with its run.
    asked = get(runtime, task["id"])
    assert asked["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    data = last_data(asked)
    assert (data["request_id"], data["response_schema"]) == (request_id, QUESTION)
    # An answer that doesn't fit asks again.
    again = send(
        runtime,
        {"data": {"send": "yes"}},
        taskId=task["id"],
        contextId=task["contextId"],
    )
    assert again["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert run_of(runtime, task)["status"] == "paused"
    taken = send(
        runtime,
        {"data": {"send": True}},
        taskId=task["id"],
        contextId=task["contextId"],
    )
    assert taken["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert run_of(runtime, task)["status"] == "queued"


def test_approvals_are_decided_only_by_those_the_step_names(runtime: Runtime) -> None:
    task = send(runtime, {"data": {"key": "a1"}})["result"]["task"]
    run = run_of(runtime, task)
    runtime.paused(run["id"], "approval", approvers="org:admin")
    asked = get(runtime, task["id"])
    assert last_data(asked)["approvers"] == "org:admin"
    refused = send(
        runtime, {"text": "approve"}, taskId=task["id"], contextId=task["contextId"]
    )
    assert refused["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert (
        "never anonymously"
        in refused["result"]["task"]["status"]["message"]["parts"][0]["text"]
    )
    assert run_of(runtime, task)["status"] == "paused"
    # The admin decides it in the console; the task catches up when read.
    runtime.world.call(
        "POST",
        f"/workflows/{SHIP}/runs/{run['id']}/decisions",
        ADMIN,
        {"request_id": run_of(runtime, task)["pause"]["id"], "approved": True},
        prefix="/api/v1/runtime",
    )
    assert get(runtime, task["id"])["status"]["state"] == "TASK_STATE_WORKING"


def test_a_finished_run_completes_its_task_with_the_result(runtime: Runtime) -> None:
    task = send(runtime, {"data": {"key": "a1"}})["result"]["task"]
    run = run_of(runtime, task)
    assert runtime.call(runtime.runs.claim, run["id"], owner="w1", lease_seconds=60)
    runtime.call(
        runtime.runs.finish,
        run["id"],
        owner="w1",
        state={},
        succeeded=True,
        result={"shipped": True},
        error=None,
    )
    done = get(runtime, task["id"])
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["artifacts"][0]["name"] == "result"
    assert done["artifacts"][0]["parts"][0]["data"] == {"shipped": True}
    # Read again, it's as it was.
    assert get(runtime, task["id"])["artifacts"] == done["artifacts"]


def test_cancelling_the_task_gives_its_run_up(runtime: Runtime) -> None:
    task = send(runtime, {"data": {"key": "a1"}})["result"]["task"]
    cancelled = rpc(runtime, "CancelTask", {"id": task["id"]})["result"]
    assert cancelled["status"]["state"] == "TASK_STATE_CANCELED"
    assert run_of(runtime, task)["status"] == "abandoned"


def test_tasks_are_their_callers(runtime: Runtime) -> None:
    world: World = runtime.world
    secret = world.make_key(name="CI")["secret"]
    message = {
        "messageId": "m-key",
        "role": "ROLE_USER",
        "parts": [{"data": {"key": "a1"}}],
    }
    task = rpc(runtime, "SendMessage", {"message": message}, key=secret)["result"][
        "task"
    ]
    assert run_of(runtime, task)["requested_by_name"] == "API key CI"
    # Not found by anyone else.
    other = rpc(runtime, "GetTask", {"id": task["id"]})
    assert "error" in other
    assert "result" in rpc(runtime, "GetTask", {"id": task["id"]}, key=secret)
