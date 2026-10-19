"""What callers see of a conversation (.views): the full view and the public one."""

from types import SimpleNamespace
from typing import Any

from google.adk.events import Event, EventActions
from google.adk.sessions import Session
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from forge_agent_runtime.document import parse_document
from forge_agent_runtime.tasks import TASK_CALLS_KEY
from forge_agent_runtime.tools import GuardedTools
from forge_agent_runtime.views import (
    FAILED,
    FULL_VIEW,
    MODEL_FAILED,
    MODEL_SIGN_IN_FAILED,
    SHOWN_CALLS_KEY,
    Projector,
    ShownCallsPlugin,
    public_error,
    public_view,
)
from forge_common.adk.toolset import SHOWN_ATTR
from forge_common.adk.turns import TURN_KEY


def doc(**state_properties: Any):
    return parse_document(
        {
            "format": "forge.chat_agent/v1",
            "id": "ca_views",
            "name": "Views",
            "nodes": [
                {
                    "id": "entry",
                    "kind": "agent",
                    "name": "Support",
                    "config": {
                        "instruction": "Help.",
                        "state_schema": {"type": "object", "properties": state_properties},
                    },
                }
            ],
            "edges": [],
        }
    )


def parts(event: Event) -> list[types.Part]:
    assert event.content is not None and event.content.parts is not None
    return event.content.parts


def call_of(event: Event) -> types.FunctionCall:
    call = parts(event)[0].function_call
    assert call is not None
    return call


def answer_of(event: Event) -> types.FunctionResponse:
    answer = parts(event)[0].function_response
    assert answer is not None
    return answer


def calling(name: str, call_id: str, **args: Any) -> Event:
    return Event(
        author="support",
        invocation_id="i1",
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))],
        ),
    )


def answering(name: str, call_id: str, response: dict[str, Any], author: str = "support") -> Event:
    return Event(
        author=author,
        invocation_id="i1",
        content=types.Content(
            role="user",
            parts=[
                types.Part(function_response=types.FunctionResponse(id=call_id, name=name, response=response))
            ],
        ),
    )


def tagged(event: Event, key: str, ids: list[str]) -> Event:
    event.custom_metadata = {**(event.custom_metadata or {}), key: ids}
    return event


def test_the_full_view_shows_everything_as_it_is():
    projector = Projector(FULL_VIEW)
    event = calling("look_up_order", "c1", order_id="A1")
    assert projector.event(event) is event


def test_a_person_isnt_shown_the_models_thoughts():
    event = Event(
        author="support",
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="Let me think about the refund policy.", thought=True),
                types.Part(text="Done."),
            ],
        ),
    )
    shown = Projector(public_view(doc())).event(event)
    assert [part.text for part in parts(shown)] == ["Done."]
    assert event.content is not None
    assert len(event.content.parts or ()) == 2  # The session's own is left as it is.


def test_a_reply_of_only_thoughts_shows_no_content():
    event = Event(
        author="support", content=types.Content(role="model", parts=[types.Part(text="hm", thought=True)])
    )
    assert Projector(public_view(doc())).event(event).content is None


def test_a_tools_call_and_answer_pair_up_without_what_they_carry():
    projector = Projector(public_view(doc()))
    call = projector.event(calling("look_up_order", "c1", order_id="A1"))
    answer = projector.event(
        answering("look_up_order", "c1", {"status": "success", "payload": {"secret": 1}})
    )
    shown_call = call_of(call)
    shown_answer = answer_of(answer)
    assert (shown_call.id, shown_call.name, shown_call.args) == ("c1", "look_up_order", {})
    assert (shown_answer.id, shown_answer.name, shown_answer.response) == ("c1", "look_up_order", {})


def test_a_shown_tools_call_and_answer_are_shown_whole_even_turns_apart():
    # The call was tagged as it was made; its answer comes in a later turn.
    earlier = [tagged(calling("refunds", "c2", order="A1"), SHOWN_CALLS_KEY, ["c2"])]
    projector = Projector(public_view(doc()), earlier)
    answer = projector.event(answering("refunds", "c2", {"status": "success", "payload": {"run_id": "r1"}}))
    assert (answer_of(answer).response or {})["payload"] == {"run_id": "r1"}
    call = Projector(public_view(doc())).event(earlier[0])
    assert call_of(call).args == {"order": "A1"}


def test_asking_the_person_and_handing_back_a_task_are_shown_by_name():
    projector = Projector(public_view(doc()))
    asked = projector.event(calling("adk_request_input", "c3", message="Which order?"))
    finished = projector.event(calling("finish_task", "c4", record={"order": "A1"}))
    assert call_of(asked).args == {"message": "Which order?"}
    assert call_of(finished).args == {"record": {"order": "A1"}}


def test_a_task_handed_off_is_shown():
    projector = Projector(public_view(doc()))
    handed = projector.event(tagged(calling("intake", "c5", request="New claim"), TASK_CALLS_KEY, ["c5"]))
    assert call_of(handed).args == {"request": "New claim"}


def test_what_the_person_answers_is_theirs_to_see():
    projector = Projector(public_view(doc()))
    answer = projector.event(answering("adk_request_confirmation", "c6", {"confirmed": True}, author="user"))
    assert answer_of(answer).response == {"confirmed": True}


def test_only_the_state_a_chat_reads_is_shown():
    projector = Projector(public_view(doc(customer_tier={"type": "string"})))
    event = Event(
        author="support",
        actions=EventActions(
            state_delta={
                "forge:follow_ups": None,
                "forge:context:support": {"tokens": 10},
                "forge:intake:claims": {"ssn": "123"},
                "forge:requests": ["r1"],
                "forge:summaries:support": "secret",
                "temp:request": {},
                "customer_tier": "pro",
                "model": "openai/gpt-x",
            }
        ),
    )
    shown = projector.event(event).actions.state_delta
    assert shown == {
        "forge:follow_ups": None,
        "forge:context:support": {"tokens": 10},
        "customer_tier": "pro",
        "model": "openai/gpt-x",
    }


def test_a_conversation_is_shown_with_its_title_and_without_what_the_agent_keeps():
    session = Session(
        id="s1",
        app_name="ca_views",
        user_id="ada",
        state={"forge:title": "Order A1", "forge:intake:claims": {"ssn": "123"}, "user:plan": "pro"},
        events=[
            tagged(calling("refunds", "c7", order="A1"), SHOWN_CALLS_KEY, ["c7"]),
            calling("look_up_order", "c8", order_id="A1"),
        ],
    )
    shown = Projector(public_view(doc())).session(session)
    assert shown.state == {"forge:title": "Order A1"}
    assert call_of(shown.events[0]).args == {"order": "A1"}
    assert call_of(shown.events[1]).args == {}
    assert session.state["forge:intake:claims"] == {"ssn": "123"}  # Kept as it was.


def test_a_failure_is_said_without_why():
    projector = Projector(public_view(doc()))
    failed = projector.event(
        Event(
            author="support",
            error_code="INVALID_REQUEST",
            error_message="sqlalchemy.exc.OperationalError: ...",
            custom_metadata={
                TURN_KEY: {
                    "version": 1,
                    "status": "failed",
                    "errorCode": "INVALID_REQUEST",
                    "errorMessage": "raw",
                },
                "forge:usage": {"tokens": 3},
            },
        )
    )
    assert failed.error_code == "INVALID_REQUEST"
    assert failed.error_message == FAILED
    assert failed.custom_metadata == {
        TURN_KEY: {"version": 1, "status": "failed", "errorCode": "INVALID_REQUEST", "errorMessage": FAILED}
    }


def test_failures_are_named_for_what_failed():
    assert public_error("ACCESS_DENIED", "You need the Support group.") == "You need the Support group."
    assert public_error("MODEL_AUTH_FAILED", "token expired for key sk-…") == MODEL_SIGN_IN_FAILED
    assert public_error("RESOURCE_EXHAUSTED", "quota for project 123") == MODEL_FAILED
    assert public_error("MODEL_ERROR_503", "upstream said …") == MODEL_FAILED
    assert public_error("PROVIDER_UNAVAILABLE", "…") == MODEL_FAILED
    assert public_error("INTERNAL_ERROR", "Traceback …") == FAILED
    assert public_error(None, None) is None


class _Tool(BaseTool):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=name)


async def test_the_calls_of_shown_tools_are_tagged_as_theyre_made():
    plugin = ShownCallsPlugin()
    shown = _Tool("refunds")
    setattr(shown, SHOWN_ATTR, True)
    request: Any = SimpleNamespace(tools_dict={"refunds": shown, "look_up_order": _Tool("look_up_order")})
    context: Any = SimpleNamespace(invocation_id="i1", agent_name="support")
    assert await plugin.before_model_callback(callback_context=context, llm_request=request) is None

    event = Event(
        author="support",
        invocation_id="i1",
        content=types.Content(
            role="model",
            parts=[
                types.Part(function_call=types.FunctionCall(id="a", name="refunds", args={})),
                types.Part(function_call=types.FunctionCall(id="b", name="look_up_order", args={})),
                types.Part(function_call=types.FunctionCall(id="c", name="adk_request_input", args={})),
            ],
        ),
    )
    invocation: Any = SimpleNamespace(invocation_id="i1")
    # Tagged where it is, so the plugins after it are asked too.
    assert await plugin.on_event_callback(invocation_context=invocation, event=event) is None
    assert event.custom_metadata == {SHOWN_CALLS_KEY: ["a", "c"]}

    await plugin.after_run_callback(invocation_context=invocation)
    assert not plugin._tools


async def test_a_toolset_its_node_shows_marks_its_tools():
    shown = await GuardedTools([_Tool("look_up_order")], shown=True).get_tools()
    hidden = await GuardedTools([_Tool("look_up_order")]).get_tools()
    assert getattr(shown[0], SHOWN_ATTR, False) is True
    assert getattr(hidden[0], SHOWN_ATTR, False) is False
