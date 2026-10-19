import asyncio

import anyio
import pytest
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types

from forge_common.adk import turns
from forge_common.adk.turns import TURN_KEY, TurnOutcome, persist_turn


async def save(outcome, sessions, invocation_id=None):
    await persist_turn(
        outcome,
        sessions=sessions,
        app_name="app",
        user_id="u",
        session_id="s",
        author="agent",
        invocation_id=invocation_id,
    )


@pytest.fixture
async def sessions():
    service = InMemorySessionService()
    await service.create_session(app_name="app", user_id="u", session_id="s")
    return service


async def history(sessions):
    return await sessions.get_session(app_name="app", user_id="u", session_id="s")


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_terminal_is_correlated_contentless_and_idempotent(sessions, status):
    outcome = TurnOutcome(status=status, error_code="FAILED" if status == "failed" else None)
    response = Event(
        author="agent",
        invocation_id="actual-invocation",
        content=types.Content(role="model", parts=[types.Part(text="Saved answer")]),
    )
    await sessions.append_event(session=await history(sessions), event=response)
    outcome.observe(response)
    await asyncio.gather(
        save(outcome, sessions, "request-id"), save(outcome, sessions, "request-id")
    )
    stored = await history(sessions)
    assert len(stored.events) == 2
    terminal = stored.events[-1]
    assert terminal.invocation_id == "actual-invocation"
    assert terminal.custom_metadata[TURN_KEY]["runId"] == outcome.run_id
    assert terminal.custom_metadata[TURN_KEY]["status"] == status
    assert terminal.content is None
    assert not terminal.actions.state_delta


async def test_success_never_adds_history_or_even_reads_the_store(sessions, monkeypatch):
    async def refused(**kwargs):
        raise AssertionError("Successful turns need no history persistence")

    monkeypatch.setattr(sessions, "get_session", refused)
    await save(TurnOutcome(status="succeeded"), sessions)


async def test_native_error_keeps_its_text_and_gains_only_a_contentless_terminal(sessions):
    outcome = TurnOutcome()
    event = Event(
        author="agent", invocation_id="current", error_code="SAFETY", error_message="Blocked."
    )
    await sessions.append_event(session=await history(sessions), event=event)
    outcome.observe(event)
    await save(outcome, sessions)
    stored = (await history(sessions)).events
    assert stored[0] == event
    assert len(stored) == 2
    assert stored[1].content is None
    assert stored[1].error_code is None
    assert stored[1].custom_metadata[TURN_KEY]["errorMessage"] == "Blocked."


async def test_run_id_prevents_duplicate_marker_when_invocation_correlation_changes(sessions):
    original = TurnOutcome(status="failed", invocation_id="first")
    await save(original, sessions)
    retried = TurnOutcome(run_id=original.run_id, status="failed", invocation_id="other")
    await save(retried, sessions)
    assert len((await history(sessions)).events) == 1


@pytest.mark.parametrize("known_id", [None, "request-id"])
async def test_unknown_invocation_does_not_attach_to_an_older_or_newer_turn(sessions, known_id):
    await sessions.append_event(
        session=await history(sessions),
        event=Event(author="agent", invocation_id="other-turn", error_code="OLDER_ERROR"),
    )
    outcome = TurnOutcome(status="failed", error_code="CURRENT_ERROR")
    await sessions.append_event(
        session=await history(sessions),
        event=Event(author="user", invocation_id="newer-turn"),
    )
    await save(outcome, sessions, known_id)
    stored = await history(sessions)
    assert len(stored.events) == 3
    assert stored.events[-1].invocation_id == (known_id or outcome.run_id)
    assert stored.events[-1].custom_metadata[TURN_KEY]["errorCode"] == "CURRENT_ERROR"


async def test_older_failure_on_a_resumed_invocation_does_not_suppress_new_cancellation(sessions):
    event = Event(author="agent", invocation_id="resumed", error_code="OLD_ERROR", timestamp=1)
    await sessions.append_event(session=await history(sessions), event=event)
    await save(TurnOutcome(status="cancelled"), sessions, "resumed")
    assert (await history(sessions)).events[-1].custom_metadata[TURN_KEY]["status"] == "cancelled"


@pytest.mark.parametrize("mode", ["asyncio", "anyio"])
async def test_disconnect_during_history_write_finishes_without_swallowing_cancellation(
    sessions, monkeypatch, mode
):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = sessions.append_event

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(sessions, "append_event", delayed)
    outcome = TurnOutcome(status="cancelled")
    scopes = []
    completed_normally = []

    async def work():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await save(outcome, sessions)
            completed_normally.append(True)
        finished.set()

    task = asyncio.create_task(work())
    await entered.wait()
    if mode == "asyncio":
        task.cancel()
    else:
        scopes[0].cancel()
    release.set()
    if mode == "asyncio":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
        assert finished.is_set()
    assert completed_normally == []
    assert (await history(sessions)).events[-1].custom_metadata[TURN_KEY]["status"] == "cancelled"


@pytest.mark.parametrize("operation", ["get_session", "append_event"])
async def test_unavailable_history_store_has_bounded_cancelled_cleanup(
    sessions, monkeypatch, operation
):
    cleaned = asyncio.Event()

    async def blocked(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(sessions, operation, blocked)
    monkeypatch.setattr(turns, "PERSIST_TIMEOUT", 0.01)
    await asyncio.wait_for(save(TurnOutcome(status="failed"), sessions), 0.5)
    await asyncio.wait_for(cleaned.wait(), 0.5)


async def test_history_write_failure_preserves_the_original_turn(sessions, monkeypatch, caplog):
    async def broken(**kwargs):
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr(sessions, "append_event", broken)
    outcome = TurnOutcome(status="failed", error_code="SAFETY", error_message="Blocked.")
    await save(outcome, sessions)
    assert (outcome.status, outcome.error_code, outcome.error_message) == (
        "failed",
        "SAFETY",
        "Blocked.",
    )
    assert "Couldn't save a turn" in caplog.text
