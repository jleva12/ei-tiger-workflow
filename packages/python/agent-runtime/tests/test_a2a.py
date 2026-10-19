import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from a2a.helpers import new_task
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TaskState, TaskStatusUpdateEvent
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from forge_agent_runtime import a2a
from forge_agent_runtime.a2a import api_key_security, create_a2a_router, owner_of, task_store
from forge_agent_runtime.executor import AgentExecutor, DirectoryAgentSource
from forge_agent_runtime.services import RuntimeServices
from forge_common.adk.turns import TURN_KEY, current_turn
from tests.conftest import EXAMPLE_HOSTS, call, text, without

AGENT = "ca_1k2cuqsuev"
STATE = {"type": "object", "properties": {"customer_tier": {"type": "string", "enum": ["free", "pro"]}}}
V1 = {"A2A-Version": "1.0"}


@pytest.fixture
def agents(tmp_path: Path, example) -> Path:
    """The example (without its MCP server, which would connect), and a second agent beside it."""
    doc = without(example, "help")
    entry = doc["nodes"][0]
    entry["config"]["state_schema"] = STATE
    entry["config"]["instruction"] = "Help {{ request.userId }} ({{ state.customer_tier }})."
    (tmp_path / "support.json").write_text(json.dumps(doc))
    other = {**without(example, "help", "memory", "orders", "billing", "billing_api"), "id": "ca_other"}
    (tmp_path / "other.json").write_text(json.dumps(other))
    return tmp_path


@pytest.fixture
def executor(agents, models, web) -> AgentExecutor:
    return AgentExecutor(
        DirectoryAgentSource(agents),
        models=models,
        http=web(),
        services=RuntimeServices(allowed_hosts=EXAMPLE_HOSTS),
    )


def app_of(executor: AgentExecutor, **options: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(create_a2a_router(executor, path="/a2a", **options))
    return app


@pytest.fixture
def client(executor):
    with TestClient(app_of(executor)) as client:
        yield client


def rpc(client, method: str, params: dict[str, Any], *, app: str = AGENT, headers=None) -> dict[str, Any]:
    response = client.post(
        f"/a2a/{app}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def message_v1(words: str, context: str | None = None, **extra: Any) -> dict[str, Any]:
    message = {"messageId": f"m-{words[:8]}", "role": "ROLE_USER", "parts": [{"text": words}], **extra}
    if context:
        message["contextId"] = context
    return {"message": message}


def test_the_card_says_what_the_agent_is_and_where_both_protocols_are(client):
    card = client.get(f"/a2a/{AGENT}/.well-known/agent-card.json").json()

    assert card["name"] == "Support assistant"
    assert card["capabilities"]["streaming"] is True
    url = f"http://testserver/a2a/{AGENT}"
    versions = {(i["url"], i["protocolBinding"], i["protocolVersion"]) for i in card["supportedInterfaces"]}
    assert versions == {(url, "JSONRPC", "1.0"), (url, "JSONRPC", "0.3")}
    # 0.3 clients read the same card.
    assert card["url"] == url
    assert card["preferredTransport"] == "JSONRPC"
    assert card["skills"][0]["name"] == "Support assistant"
    assert "Billing specialist" in {skill["name"] for skill in card["skills"]}


def test_a_pinned_version_or_the_draft_has_a_card_of_its_own(client):
    assert client.get(f"/a2a/{AGENT}@7/.well-known/agent-card.json").status_code == 404
    assert client.get("/a2a/ca_nobody/.well-known/agent-card.json").status_code == 404
    assert client.post("/a2a/ca_nobody", json={}).status_code == 404


def test_a_1_0_message_runs_a_turn_and_completes_with_the_reply(client, llm):
    llm.turns = [[text("Hello! How can I help?")]]
    got = rpc(client, "SendMessage", message_v1("Hi", "conv-1"), headers=V1)

    task = got["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["contextId"] == "conv-1"
    assert task["artifacts"][0]["parts"][0]["text"] == "Hello! How can I help?"
    # The conversation is the agent's session, with ADK's A2A user for an unknown caller.
    assert llm.requests[0].config.system_instruction.startswith("Help A2A_USER_conv-1 ().")

    fetched = rpc(client, "GetTask", {"id": task["id"]}, headers=V1)["result"]
    assert fetched["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_the_conversation_carries_on_in_its_context(client, executor, llm):
    llm.turns = [[text("Noted.")], [text("You said hi.")]]
    rpc(client, "SendMessage", message_v1("Hi", "conv-2"), headers=V1)
    rpc(client, "SendMessage", message_v1("What did I say?", "conv-2"), headers=V1)

    second = llm.requests[1]
    said = [part.text for content in second.contents for part in content.parts or [] if part.text]
    assert said[:3] == ["Hi", "Noted.", "What did I say?"]
    session = await executor.sessions.get_session(
        app_name=AGENT, user_id="A2A_USER_conv-2", session_id="conv-2"
    )
    assert session is not None


def test_a_0_3_message_is_answered_as_0_3(client, llm):
    llm.turns = [[text("Hello from 0.3.")]]
    got = rpc(
        client,
        "message/send",
        {
            "message": {
                "kind": "message",
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "text", "text": "Hi"}],
                "contextId": "conv-3",
            }
        },
    )

    task = got["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0] == {"kind": "text", "text": "Hello from 0.3."}
    assert rpc(client, "tasks/get", {"id": task["id"]})["result"]["status"]["state"] == "completed"


def test_streaming_sends_the_task_as_it_goes(client, llm):
    llm.turns = [[call("look_up_order", order_id="A1")], [text("It shipped.")]]
    with client.stream(
        "POST",
        f"/a2a/{AGENT}",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage", "params": message_v1("A1?", "c4")},
        headers=V1,
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]

    results = [event["result"] for event in events]
    assert "task" in results[0]
    states = [r["statusUpdate"]["status"]["state"] for r in results if "statusUpdate" in r]
    assert states[0] == "TASK_STATE_WORKING"
    assert states[-1] == "TASK_STATE_COMPLETED"
    assert any("artifactUpdate" in r for r in results)


def test_state_comes_in_the_message_metadata_and_is_checked(client, llm):
    llm.turns = [[text("Hi, pro.")]]
    rpc(
        client,
        "SendMessage",
        message_v1("Hi", "c5", metadata={"state": {"customer_tier": "pro"}}),
        headers=V1,
    )
    assert llm.requests[0].config.system_instruction.startswith("Help A2A_USER_c5 (pro).")

    refused = rpc(
        client, "SendMessage", message_v1("Hi", "c6", metadata={"state": {"plan": "gold"}}), headers=V1
    )
    task = refused["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "plan" in task["status"]["message"]["parts"][0]["text"]


def test_a_context_id_a_conversation_cant_have_is_refused_before_a_task_is_kept(client, llm):
    got = rpc(client, "SendMessage", message_v1("Hi", "x" * 40), headers=V1)
    assert got["error"]["code"] == -32602 and "36 at most" in got["error"]["message"]
    old = rpc(
        client,
        "message/send",
        {
            "message": {
                "kind": "message",
                "messageId": "m",
                "role": "user",
                "parts": [{"kind": "text", "text": "Hi"}],
                "contextId": "a b",
            }
        },
    )
    # a2a-sdk's 0.3 adapter answers A2A's own errors as internal ones, saying why.
    assert "36 at most" in old["error"]["message"]
    assert llm.requests == []


def test_a_model_failure_fails_the_task_saying_why(client, llm):
    llm.turns = [ValueError("The prompt is too long.")]
    task = rpc(client, "SendMessage", message_v1("Hi", "c7"), headers=V1)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"] == "The prompt is too long."


def model_replies(monkeypatch, llm, replies):
    """Native model responses through the real ProviderModels and agent runner."""

    async def generate(self, request, stream=False):
        self.requests.append(request)
        for response in replies:
            if isinstance(response, Exception):
                raise response
            yield response

    monkeypatch.setattr(type(llm), "generate_content_async", generate)


@pytest.mark.parametrize("streaming", [False, True])
def test_a_native_truncated_reply_fails_with_its_partial_artifact(client, llm, monkeypatch, streaming):
    response = LlmResponse.create(
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[text("This answer was cut off")]),
                    finish_reason=types.FinishReason.MAX_TOKENS,
                )
            ]
        )
    )
    # Gemini supplies the stop reason without error_code when it produced text.
    assert response.error_code is None
    model_replies(monkeypatch, llm, [response])
    if streaming:
        with client.stream(
            "POST",
            f"/a2a/{AGENT}",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendStreamingMessage",
                "params": message_v1("Hi", "truncated"),
            },
            headers=V1,
        ) as reply:
            events = [
                json.loads(line[5:])["result"] for line in reply.iter_lines() if line.startswith("data:")
            ]
        states = [event["statusUpdate"]["status"]["state"] for event in events if "statusUpdate" in event]
        assert states[-1] == "TASK_STATE_FAILED"
        assert states.count("TASK_STATE_FAILED") == 1
        assert "TASK_STATE_COMPLETED" not in states
        assert any("artifactUpdate" in event for event in events[:-1])
        task_id = events[0]["task"]["id"]
        task = rpc(client, "GetTask", {"id": task_id}, headers=V1)["result"]
    else:
        task = rpc(client, "SendMessage", message_v1("Hi", "truncated"), headers=V1)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"]
    assert task["artifacts"][0]["parts"] == [{"text": "This answer was cut off"}]
    assert task["artifacts"][0]["metadata"]["forge"]["incomplete"] is True


@pytest.mark.parametrize(
    "replies",
    [
        [],
        [LlmResponse(content=types.Content(role="model", parts=[]))],
        [LlmResponse(content=types.Content(role="model", parts=[text("Unfinished")]), partial=True)],
    ],
)
def test_an_empty_or_unfinished_model_turn_fails_instead_of_completing(client, llm, monkeypatch, replies):
    model_replies(monkeypatch, llm, replies)
    task = rpc(client, "SendMessage", message_v1("Hi", "unfinished"), headers=V1)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"]
    if replies and replies[0].partial:
        assert task["artifacts"][0]["parts"] == [{"text": "Unfinished"}]


def test_a_model_connection_failure_keeps_the_text_already_received(client, llm, monkeypatch):
    model_replies(
        monkeypatch,
        llm,
        [
            LlmResponse(content=types.Content(role="model", parts=[text("Already written")]), partial=True),
            ConnectionError("The model connection closed."),
        ],
    )
    task = rpc(client, "SendMessage", message_v1("Hi", "disconnected"), headers=V1)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "Try again" in task["status"]["message"]["parts"][0]["text"]
    assert task["artifacts"][0]["parts"] == [{"text": "Already written"}]


@pytest.mark.parametrize("legacy", [False, True])
def test_an_error_on_a_partial_model_response_is_not_dropped(client, llm, monkeypatch, legacy):
    model_replies(
        monkeypatch,
        llm,
        [
            LlmResponse(
                content=types.Content(role="model", parts=[text("Partial response")]),
                partial=True,
                error_code="PROVIDER_UNAVAILABLE",
                error_message="The model's service became unavailable. Please retry.",
            )
        ],
    )
    if legacy:
        params = {
            "message": {
                "kind": "message",
                "messageId": "partial-error",
                "role": "user",
                "parts": [{"kind": "text", "text": "Hi"}],
                "contextId": "partial-error",
            }
        }
        task = rpc(client, "message/send", params)["result"]
        assert task["status"]["state"] == "failed"
    else:
        task = rpc(client, "SendMessage", message_v1("Hi", "partial-error"), headers=V1)["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"] == (
        "The model's service became unavailable. Please retry."
    )
    assert task["artifacts"][0]["parts"][0]["text"] == "Partial response"
    assert task["artifacts"][0]["metadata"]["forge"]["incomplete"] is True


def test_a_later_connection_error_preserves_the_original_failure_and_audit(
    client, executor, llm, monkeypatch
):
    audited = []
    explanation = "The model's service became unavailable. Please retry."

    class Observer:
        def plugins(self, agent):
            return ()

        @asynccontextmanager
        async def invocation(self, agent, request):
            try:
                yield
            finally:
                audited.append((current_turn().status, current_turn().error_message))

    executor.observer = Observer()
    model_replies(
        monkeypatch,
        llm,
        [
            LlmResponse(
                content=types.Content(role="model", parts=[text("Partial response")]),
                partial=True,
                error_code="PROVIDER_UNAVAILABLE",
                error_message=explanation,
            ),
            ConnectionError("The model connection closed after its error."),
        ],
    )

    task = rpc(client, "SendMessage", message_v1("Hi", "native-before-close"), headers=V1)["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"][0]["text"] == explanation
    assert task["artifacts"][0]["parts"] == [{"text": "Partial response"}]
    assert audited == [("failed", explanation)]


def test_a_native_interruption_cancels_the_task_and_audit_with_partial_output(
    client, executor, llm, monkeypatch
):
    audited = []

    class Observer:
        def plugins(self, agent):
            return ()

        @asynccontextmanager
        async def invocation(self, agent, request):
            yield
            audited.append(current_turn().status)

    executor.observer = Observer()
    model_replies(
        monkeypatch,
        llm,
        [
            LlmResponse(
                content=types.Content(role="model", parts=[text("Interrupted output")]),
                interrupted=True,
            )
        ],
    )

    task = rpc(client, "SendMessage", message_v1("Hi", "interrupted"), headers=V1)["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_CANCELED"
    assert "stopped before it finished" in task["status"]["message"]["parts"][0]["text"]
    assert task["artifacts"][0]["parts"] == [{"text": "Interrupted output"}]
    assert task["artifacts"][0]["metadata"]["forge"]["incomplete"] is True
    assert audited == ["cancelled"]


@pytest.mark.parametrize("failure", ["convert", "enqueue"])
async def test_transport_failures_close_the_executor_with_a_failed_audit(
    client, executor, llm, monkeypatch, failure
):
    audited = []
    restored = []
    saved_before_terminal = []
    execute = a2a.ForgeA2aExecutor.execute

    async def checked_execute(self, context, event_queue):
        previous = current_turn()
        enqueue = event_queue.enqueue_event
        failed = False

        async def checked_enqueue(event):
            nonlocal failed
            if isinstance(event, TaskStatusUpdateEvent):
                if (
                    failure == "enqueue"
                    and not failed
                    and event.status.state == TaskState.TASK_STATE_WORKING
                    and event.status.message.parts
                ):
                    failed = True
                    raise ValueError("The reply could not be queued.")
                if event.status.state == TaskState.TASK_STATE_FAILED:
                    kept = await executor.sessions.get_session(
                        app_name=AGENT,
                        user_id="A2A_USER_transport-failed",
                        session_id="transport-failed",
                    )
                    saved_before_terminal.append(
                        any(TURN_KEY in (item.custom_metadata or {}) for item in kept.events)
                    )
            await enqueue(event)

        monkeypatch.setattr(event_queue, "enqueue_event", checked_enqueue)
        try:
            await execute(self, context, event_queue)
        finally:
            restored.append(current_turn() is previous)

    monkeypatch.setattr(a2a.ForgeA2aExecutor, "execute", checked_execute)

    class Observer:
        def plugins(self, agent):
            return ()

        @asynccontextmanager
        async def invocation(self, agent, request):
            try:
                yield
            finally:
                audited.append(current_turn().status)

    executor.observer = Observer()
    llm.turns = [[text("Output retained")]]
    if failure == "convert":

        def refused(*args, **kwargs):
            raise ValueError("The reply could not be converted.")

        monkeypatch.setattr(a2a, "convert_event_to_a2a_events", refused)
    task = rpc(client, "SendMessage", message_v1("Hi", "transport-failed"), headers=V1)["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["artifacts"][0]["parts"] == [{"text": "Output retained"}]
    # The executor has closed in the transport's task before it sends failure.
    assert audited == ["failed"]
    assert restored == [True]
    assert saved_before_terminal == [True]
    history = await executor.sessions.get_session(
        app_name=AGENT,
        user_id="A2A_USER_transport-failed",
        session_id="transport-failed",
    )
    markers = [event for event in history.events if TURN_KEY in (event.custom_metadata or {})]
    assert len(markers) == 1
    marker = markers[0].custom_metadata[TURN_KEY]
    assert marker["status"] == "failed"
    assert marker["errorMessage"] == task["status"]["message"]["parts"][0]["text"]
    answer = next(event for event in history.events if event.content and event.content.role == "model")
    assert markers[0].invocation_id == answer.invocation_id


def test_a_tool_confirmation_still_waits_for_input(client, llm, agents):
    path = agents / "support.json"
    document = json.loads(path.read_text())
    next(node for node in document["nodes"] if node["id"] == "orders")["config"]["confirm"] = True
    path.write_text(json.dumps(document))
    llm.turns = [[call("look_up_order", order_id="A1")]]

    task = rpc(client, "SendMessage", message_v1("A1?", "confirmation"), headers=V1)["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["status"]["message"]["parts"]


def test_an_executor_that_ends_without_events_does_not_complete_an_empty_task(client, executor, monkeypatch):
    async def empty(*args, **kwargs):
        if False:
            yield

    monkeypatch.setattr(executor, "run", empty)
    task = rpc(client, "SendMessage", message_v1("Hi", "empty-executor"), headers=V1)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "without returning a reply" in task["status"]["message"]["parts"][0]["text"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["task_store", "service"])
def test_unexpected_protocol_failures_are_logged_and_safe_for_clients(
    executor, caplog, legacy, streaming, failure
):
    secret = "mysql://user:private-password@database/internal"

    class BrokenStore(InMemoryTaskStore):
        async def get(self, task_id, context):
            raise RuntimeError(secret)

    class BrokenServiceHandler(a2a.ForgeRequestHandler):
        async def on_message_send(self, params, context):
            raise RuntimeError(secret)

        async def on_message_send_stream(self, params, context):
            yield new_task("service-task", "service-context", TaskState.TASK_STATE_WORKING)
            raise RuntimeError(secret)

    async def find(request, name, action):
        return name

    async def card(request, name, url):
        return a2a._GENERIC_CARD

    options = {}
    if failure == "task_store":
        options["tasks"] = BrokenStore()
        target = AGENT
        method = (
            ("tasks/resubscribe" if streaming else "tasks/get")
            if legacy
            else ("SubscribeToTask" if streaming else "GetTask")
        )
        params = {"id": "stored-task"}
    else:
        options["services"] = [
            a2a.A2aService(
                prefix="ag_",
                find=find,
                card=card,
                executor=a2a.ForgeA2aExecutor(executor),
                handler=BrokenServiceHandler,
            )
        ]
        target = "ag_broken"
        method = (
            ("message/stream" if streaming else "message/send")
            if legacy
            else ("SendStreamingMessage" if streaming else "SendMessage")
        )
        params = (
            {
                "message": {
                    "kind": "message",
                    "messageId": "failure",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Hi"}],
                }
            }
            if legacy
            else message_v1("Hi")
        )

    with TestClient(app_of(executor, **options)) as client:
        response = client.post(
            f"/a2a/{target}",
            headers={} if legacy else V1,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )

    assert response.status_code == 200
    assert secret not in response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        messages = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
        if failure == "service":
            assert "result" in messages[0]
        error = messages[-1]["error"]
    else:
        error = response.json()["error"]
    assert error["code"] == -32603
    assert error["message"] == "The agent service ran into a problem. Please try again."
    assert any(record.exc_info and secret in str(record.exc_info[1]) for record in caplog.records)


def test_a_task_is_found_only_through_its_own_agent(client, llm):
    llm.turns = [[text("Hi.")]]
    task = rpc(client, "SendMessage", message_v1("Hi", "c8"), headers=V1)["result"]["task"]

    other = rpc(client, "GetTask", {"id": task["id"]}, app="ca_other", headers=V1)
    assert other["error"]["code"] == -32001  # TaskNotFound


def test_listing_tasks_needs_a_known_caller(executor, llm):
    llm.turns = [[text("Hi.")], [text("Hi.")]]

    def who(request: Request) -> str | None:
        return request.headers.get("x-user")

    with TestClient(app_of(executor, caller=who)) as client:
        assert "error" in rpc(client, "ListTasks", {}, headers=V1)
        rpc(client, "SendMessage", message_v1("Hi", "c9"), headers={**V1, "x-user": "ada"})
        rpc(client, "SendMessage", message_v1("Hi", "c10"), headers={**V1, "x-user": "bob"})
        listed = rpc(client, "ListTasks", {}, headers={**V1, "x-user": "ada"})["result"]["tasks"]

    assert [task["contextId"] for task in listed] == ["c9"]
    # A known caller's conversations are theirs.
    assert llm.requests[0].config.system_instruction.startswith("Help ada ().")


def test_one_agent_is_served_at_a_path_of_its_own_and_its_card_at_the_root(executor, llm):
    async def key(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer k1":
            from fastapi import HTTPException

            raise HTTPException(401)

    app = FastAPI()
    app.include_router(
        create_a2a_router(
            executor,
            path="/a2a",
            agent=AGENT,
            dependencies=[Depends(key)],
            card_dependencies=(),
            security=api_key_security(),
        )
    )
    llm.turns = [[text("Hi.")]]
    with TestClient(app) as client:
        root = client.get("/.well-known/agent-card.json").json()
        assert root == client.get("/a2a/.well-known/agent-card.json").json()
        assert root["supportedInterfaces"][0]["url"] == "http://testserver/a2a"
        assert set(root["securitySchemes"]) == {"bearer", "apiKey"}
        sent = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": message_v1("Hi", "c11")}
        assert client.post("/a2a", json=sent, headers=V1).status_code == 401
        answered = client.post("/a2a", json=sent, headers={**V1, "authorization": "Bearer k1"}).json()
    assert answered["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_tasks_are_kept_in_a_database(tmp_path, executor, llm):
    store = task_store(f"sqlite:///{tmp_path / 'tasks.db'}")
    llm.turns = [[text("Kept.")]]
    with TestClient(app_of(executor, tasks=store)) as client:
        task = rpc(client, "SendMessage", message_v1("Hi", "c12"), headers=V1)["result"]["task"]
        assert rpc(client, "GetTask", {"id": task["id"]}, headers=V1)["result"]["artifacts"]
    await store.engine.dispose()  # type: ignore[attr-defined]


def test_an_owner_is_the_agent_and_the_caller():
    from a2a.server.context import ServerCallContext

    assert owner_of(ServerCallContext(state={"forge_agent": "ca_x"})) == "ca_x/"


def test_authorize_decides_cards_and_calls(executor):
    from fastapi import HTTPException

    seen: list[tuple[str, str]] = []

    async def authorize(request, agent, *, action, user_id):
        seen.append((agent.document.id, action))
        if request.headers.get("x-who") != "friend":
            raise HTTPException(403, "Not you")

    with TestClient(app_of(executor, authorize=authorize)) as client:
        assert client.get(f"/a2a/{AGENT}/.well-known/agent-card.json").status_code == 403
        card = client.get(f"/a2a/{AGENT}/.well-known/agent-card.json", headers={"x-who": "friend"})
        assert card.status_code == 200
        refused = client.post(
            f"/a2a/{AGENT}",
            json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}},
            headers=V1,
        )
        assert refused.status_code == 403
    assert seen == [(AGENT, "card"), (AGENT, "card"), (AGENT, "a2a")]


def test_api_key_security_can_say_what_to_send():
    schemes = api_key_security(bearer="An org key", header="The org key")
    assert schemes["bearer"].http_auth_security_scheme.description == "An org key"
    assert schemes["apiKey"].api_key_security_scheme.description == "The org key"
