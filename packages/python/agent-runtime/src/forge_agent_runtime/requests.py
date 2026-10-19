"""Intake requests: intake agents used as tools.

An intake agent on an agent's ``tools`` way (rather than its ``agents`` way,
where it's handed the conversation) is used in tool mode: the agent talks
with the person itself and keeps their requests, one record per intake, with
the tools here, over the intake engine (:mod:`.intake`), which has no model
of its own in this mode.

Several requests can be open at once, to different teams' intakes. A key two
intakes share (``requester_email``) is asked once: its answer goes to every
open request that has it, and a request started later in the conversation
starts with what its keys were already given. Each request still checks its
values its own way, and says so when one doesn't fit.

- ``start_requests``: a request for each intake the person needs (or one of
  theirs still open, resumed here).
- ``answer_requests``: what the person said, to every open request that asks for it.
- ``review_requests``: each request as it stands, to read back.
- ``submit_requests``: the ready ones, once the person said yes to each read
  back (``answer_requests``' ``summary_confirmed``, when its intake confirms
  its summary); each starts the workflows on its intake's ``on_complete``
  way, exactly once, and their runs come back (with the calls a chat follows
  them by). Submitting moves a request from ``open`` to ``submitting`` with
  its runs written down (the runner's outbox), kept only if it's still open
  as it was read; only then are its runs started, and it's ``submitted``.
  Two submits at once (parallel calls, two tabs) submit it once; a submit
  cut off before its runs started is finished by the runner's upkeep.
- ``cancel_requests`` and ``list_requests``: the person's requests, here or
  from earlier conversations with the agent.

Requests are kept by a :class:`~forge_agent_runtime.services.RequestStore`
(the hosted runtime's database) by organization, agent and person, so they
outlive the conversation; without one, in the conversation's state. Each
change is kept only on the version it was read at (compare-and-set), so two
changes at once can't undo each other: the second is made again on what the
first kept. The tools of one conversation take turns in a process. What the
agent is told before each model call (the intakes, the open requests, what
to ask next) is read from the conversation's state, which every tool keeps up
to date (:data:`SNAPSHOT_KEY`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from google.adk.tools.base_tool import BaseTool
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge_agent_runtime.intake import (
    STARTING,
    Answer,
    IntakeEngine,
    IntakeField,
    IntakeRecord,
    ValueRefused,
    apply_answers,
    checked,
    latest,
    progress_of,
    value_schema,
)
from forge_agent_runtime.services import RequestConflict, StartSource

if TYPE_CHECKING:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.tools.tool_context import ToolContext

    from forge_agent_runtime.services import RequestStore

#: Where the conversation keeps its requests when nothing stores them (the
#: hosted runtime's database does), by ID.
STATE_KEY = "forge:requests"

#: Where the conversation keeps its open requests as they stand, for what the
#: agent is told before each model call.
SNAPSHOT_KEY = "forge:requests:open"

#: The most requests :func:`list_requests` reads back.
LISTED = 20

#: How often a change refused as a conflict is made again, on what's kept now.
RETRIES = 3

RequestStatus = Literal["open", "submitting", "submitted", "cancelled"]

T = TypeVar("T")


class IntakeRequest(BaseModel):
    """One request: an intake's record, kept for the person who made it."""

    model_config = ConfigDict(extra="ignore")

    id: str
    #: The intake's node's ID: it outlives renaming the node.
    intake: str
    #: The intake's node's name.
    title: str
    #: The requests started together.
    basket: str
    #: The conversation it's worked on in.
    session_id: str
    status: RequestStatus = "open"
    record: IntakeRecord = Field(default_factory=IntakeRecord)
    #: When it was made and last changed (ISO 8601), as it was stored.
    created_at: str | None = None
    updated_at: str | None = None
    #: The version it was read at: a change is kept only on it. None: new.
    version: int | None = None


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


@dataclass(frozen=True)
class Intake:
    """An intake an agent files requests to: an intake node on its Tools way."""

    #: Its node's ID, which requests keep.
    id: str
    #: Its node's name.
    title: str
    #: What it's for.
    description: str
    #: Its design, and the workflows it starts; its ADK name (``agent``) is
    #: what the agent calls it by.
    engine: IntakeEngine

    @property
    def name(self) -> str:
        return self.engine.agent


class RequestDesk:
    """
    The intakes an agent files requests to, and where its requests are kept:
    what its tools share.

    :param intakes: In the canvas's order.
    :param store: Keeps requests; None keeps them in the conversation's state.
    :param organization_id: Whose agent it is.
    """

    def __init__(
        self,
        intakes: Sequence[Intake],
        *,
        store: RequestStore | None = None,
        organization_id: str | None = None,
    ) -> None:
        #: By node ID.
        self.intakes = {intake.id: intake for intake in intakes}
        #: By ADK name.
        self.named = {intake.name: intake for intake in intakes}
        self.store = store
        self.organization_id = organization_id
        #: Each conversation's tools take turns (parallel calls of one model answer).
        self._turns: dict[str, _Turns] = {}

    def engine(self, request: IntakeRequest) -> IntakeEngine:
        return self.intakes[request.intake].engine

    # Where requests are kept -------------------------------------------------

    def _owner(self, context: ToolContext) -> dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "agent_id": context.session.app_name,
            "user_id": context.user_id,
        }

    async def load(
        self,
        context: ToolContext,
        *,
        ids: Sequence[str] | None = None,
        status: RequestStatus | None = None,
        here: bool = False,
    ) -> list[IntakeRequest]:
        """
        :param ids: Only these.
        :param status: Only those with it.
        :param here: Only those worked on in this conversation.
        :return: The person's requests with the agent, newest first; only
            those to intakes it still has.
        """
        if self.store is not None:
            raw = await self.store.requests(
                **self._owner(context), ids=ids, status=status, limit=None if ids else 200
            )
        else:
            kept = latest(context).get(STATE_KEY)
            raw = list(kept.values()) if isinstance(kept, Mapping) else []
        found: list[IntakeRequest] = []
        for each in raw:
            try:
                request = IntakeRequest.model_validate(each)
            except ValidationError:
                continue
            intake = self.intakes.get(request.intake)
            if intake is None:
                continue
            if ids is not None and request.id not in ids:
                continue
            if status is not None and request.status != status:
                continue
            if here and request.session_id != context.session.id:
                continue
            # As its node is called now.
            request.title = intake.title
            found.append(request)
        found.sort(key=lambda request: request.updated_at or "", reverse=True)
        return found

    async def save(self, context: ToolContext, requests: Sequence[IntakeRequest]) -> None:
        """
        Keeps the requests, each on the version it was read at: all of them,
        each at its next version, or none.

        :raises RequestConflict: One changed since it was read.
        """
        stamp = now()
        for request in requests:
            request.created_at = request.created_at or stamp
            request.updated_at = stamp
        if self.store is not None:
            await self.store.save(
                [request.model_dump(mode="json") for request in requests], **self._owner(context)
            )
        else:
            kept = latest(context).get(STATE_KEY)
            merged = dict(kept) if isinstance(kept, Mapping) else {}
            for request in requests:
                there = merged.get(request.id)
                at = there.get("version") if isinstance(there, Mapping) else None
                if at != request.version:
                    raise RequestConflict(f"Request {request.id} changed since it was read.")
            merged.update(
                {
                    request.id: {**request.model_dump(mode="json"), "version": _next(request.version)}
                    for request in requests
                }
            )
            context.state[STATE_KEY] = merged
        for request in requests:
            request.version = _next(request.version)

    async def retrying(self, context: ToolContext, change: Callable[[], Awaitable[T]]) -> T:
        """
        Makes a change (reading what it changes, then keeping it) while the
        conversation's other tools wait; one refused as a conflict (another
        conversation, another process changed a request) is made again on
        what's kept now.
        """
        session_id = context.session.id
        turns = self._turns.setdefault(session_id, _Turns())
        turns.users += 1
        try:
            async with turns.lock:
                for attempt in range(RETRIES):
                    try:
                        return await change()
                    except RequestConflict:
                        if attempt == RETRIES - 1:
                            raise
                raise AssertionError("unreachable")
        finally:
            turns.users -= 1
            if not turns.users:
                self._turns.pop(session_id, None)

    # Where they stand --------------------------------------------------------

    def plan(self, requests: Sequence[IntakeRequest]) -> dict[str, Any]:
        """
        :return: The open requests and what to ask next, each key once with
            the requests that need it; a key two intakes take differently
            says how each does.
        """
        ask: dict[str, dict[str, Any]] = {}
        listed: list[dict[str, Any]] = []
        for request in requests:
            progress = progress_of(self.engine(request).design, request.record)
            for field in progress.ask_next:
                entry = ask.get(field.key)
                if entry is None:
                    ask[field.key] = {**_described(field), "for": [request.title]}
                    continue
                entry["for"].append(request.title)
                if (field.type, _options(field)) != (entry["type"], entry.get("options")):
                    entry.setdefault("differs", []).append(
                        {"for": request.title, "type": field.type, **_options_of(field)}
                    )
            engine = self.engine(request)
            listed.append(
                {
                    "id": request.id,
                    "intake": request.title,
                    "ready": progress.ready,
                    "still_needed": [field.key for field in progress.missing],
                    **(
                        {"waiting_for_yes": [field.key for field in progress.unconfirmed]}
                        if progress.unconfirmed
                        else {}
                    ),
                    **(
                        {"summary_confirmed": request.record.summary_confirmed}
                        if engine.design.confirm_summary
                        else {}
                    ),
                }
            )
        return {"requests": listed, "ask_next": list(ask.values())}

    async def refresh(self, context: ToolContext) -> dict[str, Any]:
        """:return: The open requests here and what to ask next, kept in the state for the agent's brief."""
        plan = self.plan(await self.load(context, status="open", here=True))
        context.state[SNAPSHOT_KEY] = plan
        return plan

    def brief(self, snapshot: Any) -> str:
        """What the agent is told before each model call: the intakes, and where its open requests stand."""
        lines = [
            "# Intake requests",
            "",
            "You file the person's requests to the teams' intakes below, and ask their questions yourself. "
            "The requests are kept for you, so trust them over the conversation.",
            "",
            "How to work:",
            "- When the person needs something one of these teams does, start a request to its intake "
            "(start_requests), several at once when they need several.",
            "- Ask what's next, a few related things at a time, in plain words: a key several requests need "
            "is asked once. Don't show them keys or request IDs.",
            "- As soon as they tell you anything a request asks for, record it (answer_requests): one answer "
            "goes to every open request that asks for it.",
            "- When requests are ready, read each back (review_requests) and ask if it's right. When they say "
            "yes, record it (answer_requests with summary_confirmed, for the requests they confirmed), then "
            "submit them (submit_requests); the workflows each starts show in the chat.",
            "- If they want to drop one, cancel it. If they ask about an earlier request, list_requests "
            "finds theirs; one still open can be picked up here (start_requests with resume).",
            "",
            "## The intakes",
        ]
        for intake in self.intakes.values():
            fields = ", ".join(f"{field.title} ({field.key})" for field in intake.engine.design.fields[:16])
            about = f" {intake.description.strip()}" if intake.description.strip() else ""
            lines.append(f"- {intake.name} ({intake.title}):{about} It asks for: {fields}.")
        requests = snapshot.get("requests") if isinstance(snapshot, Mapping) else None
        if requests:
            lines.extend(["", "## Open requests in this conversation"])
            for request in requests:
                if not request.get("ready"):
                    state = "still needs " + ", ".join(request.get("still_needed") or [])
                elif request.get("summary_confirmed") is False:
                    state = "ready to read back; submit once they say it's right"
                else:
                    state = "ready to submit"
                lines.append(f"- {request.get('intake')} ({request.get('id')}): {state}")
            ask = snapshot.get("ask_next") or []
            if ask:
                lines.append("")
                lines.append(
                    "Ask next: "
                    + "; ".join(
                        f"{entry['label']} ({entry['key']}, for {', '.join(entry['for'])})" for entry in ask
                    )
                )
        return "\n".join(lines)

    # What happens to them ------------------------------------------------------

    def carried(self, requests: Sequence[IntakeRequest], intake: Intake) -> dict[str, Answer]:
        """
        :param requests: Other requests here, newest first.
        :return: The answers a new request to ``intake`` starts with: what
            its keys were given in the others (the newest), as it takes them.
        """
        design = intake.engine.design
        out: dict[str, Answer] = {}
        for request in requests:
            if request.status == "cancelled":
                continue
            for key, answer in request.record.answers.items():
                field = design.field(key)
                if key in out or field is None or answer.how == "skipped":
                    continue
                try:
                    out[key] = Answer(value=checked(field, answer.value), how=answer.how)
                except ValueRefused:
                    continue  # It's asked again, as this intake takes it.
        return out


@dataclass
class _Turns:
    """A conversation's tools taking turns: who's waiting, and the lock."""

    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    users: int = 0


def _next(version: int | None) -> int:
    """:return: The version a request is at once a change to ``version`` is kept (a new one's first)."""
    return 0 if version is None else version + 1


def _options(field: IntakeField) -> list[str] | None:
    return [option.strip() for option in field.options] if field.type in ("choice", "choices") else None


def _options_of(field: IntakeField) -> dict[str, Any]:
    options = _options(field)
    return {"options": options} if options is not None else {}


def _described(field: IntakeField) -> dict[str, Any]:
    """A field as the agent is told to ask it."""
    return {
        "key": field.key,
        "label": field.title,
        "type": field.type,
        **_options_of(field),
        "required": field.required,
        **({"about": field.description.strip()} if field.description.strip() else {}),
        **({"why": field.why.strip()} if field.why.strip() else {}),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(each) for each in value] if isinstance(value, list) else []


class _DeskTool(BaseTool):
    def __init__(self, desk: RequestDesk, *, name: str, description: str) -> None:
        super().__init__(name=name, description=description)
        self.desk = desk

    def _declare(self, properties: dict[str, Any], required: Sequence[str] = ()) -> types.FunctionDeclaration:
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = list(required)
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=schema
        )

    def _keys_schema(self) -> dict[str, Any]:
        """Every key the intakes ask for, each with the first intake's schema for it."""
        properties: dict[str, Any] = {}
        for intake in self.desk.intakes.values():
            for field in intake.engine.design.fields:
                properties.setdefault(field.key, value_schema(field))
        return properties


_REQUEST_IDS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Which requests, by ID; leave it out for every open request in this conversation.",
}


class StartRequests(_DeskTool):
    def __init__(self, desk: RequestDesk) -> None:
        super().__init__(
            desk,
            name="start_requests",
            description=(
                "Starts a request to each intake the person needs, together, and picks up open requests of "
                "theirs from earlier conversations (resume). What the person already told you goes in known: "
                "each value to every request that asks for it. Answers: the requests and what to ask next."
            ),
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return self._declare(
            {
                "intakes": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(self.desk.named)},
                    "description": "The intakes to start a request to, by name.",
                },
                "known": {
                    "type": "object",
                    "properties": self._keys_schema(),
                    "additionalProperties": False,
                    "description": "What the person already told you, by key.",
                },
                "resume": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Open requests of theirs (list_requests) to pick up here, by ID.",
                },
            }
        )

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request: LlmRequest) -> None:
        await super().process_llm_request(tool_context=tool_context, llm_request=llm_request)
        # The intakes, and where the open requests stand, before each model call.
        llm_request.append_instructions([self.desk.brief(tool_context.state.get(SNAPSHOT_KEY))])

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        desk = self.desk
        names = _strings(args.get("intakes"))
        unknown = [name for name in names if name not in desk.named]
        if unknown:
            return {"error": f"There's no intake {unknown[0]!r}: it's one of {', '.join(desk.named)}."}
        basket = str(uuid.uuid4())
        ids = {name: str(uuid.uuid4()) for name in names}

        async def start() -> dict[str, Any]:
            here = await desk.load(tool_context, here=True)
            refused: dict[str, str] = {}
            changed: list[IntakeRequest] = []
            started: list[dict[str, str]] = []
            for name in names:
                intake = desk.named[name]
                request = IntakeRequest(
                    id=ids[name],
                    intake=intake.id,
                    title=intake.title,
                    basket=basket,
                    session_id=tool_context.session.id,
                )
                # What its keys were already given here, asked once.
                request.record.answers.update(desk.carried(here, intake))
                here.append(request)
                changed.append(request)
                started.append({"id": request.id, "intake": request.title})
            resumed: list[dict[str, str]] = []
            resume = _strings(args.get("resume"))
            for request in await desk.load(tool_context, ids=resume, status="open") if resume else []:
                request.session_id = tool_context.session.id
                changed.append(request)
                resumed.append({"id": request.id, "intake": request.title})
            known = _mapping(args.get("known"))
            if known:
                for request in changed:
                    applied = apply_answers(
                        desk.engine(request).design, request.record, answers=known, others=True
                    )
                    refused.update({f"{key} ({request.title})": why for key, why in applied.refused.items()})
            await desk.save(tool_context, changed)
            out: dict[str, Any] = {"started": started}
            if resumed:
                out["resumed"] = resumed
            if refused:
                out["refused"] = refused
            return out

        out = await desk.retrying(tool_context, start)
        return {**out, **await desk.refresh(tool_context)}


class AnswerRequests(_DeskTool):
    def __init__(self, desk: RequestDesk) -> None:
        super().__init__(
            desk,
            name="answer_requests",
            description=(
                "Records what the person told you in their open requests: each value goes to every request that "
                "asks for its key, checked as that request takes it. Call it as soon as they say something a "
                "request asks for. Answers: what each request took or refused, and what to ask next."
            ),
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        keys = list(self._keys_schema())
        listed = {"type": "array", "items": {"type": "string", "enum": keys}}
        return self._declare(
            {
                "answers": {
                    "type": "object",
                    "properties": self._keys_schema(),
                    "additionalProperties": False,
                    "description": "What the person said, by key. A correction replaces what was there.",
                },
                "inferred": {
                    "type": "object",
                    "properties": self._keys_schema(),
                    "additionalProperties": False,
                    "description": "What you worked out from what they said but they didn't say outright "
                    "(only fields that may be inferred take it; they'll need the person's yes).",
                },
                "confirmed": {
                    **listed,
                    "description": "Keys the person said are right, when you read them back.",
                },
                "skipped": {**listed, "description": "Optional keys the person can't or won't answer."},
                "cleared": {**listed, "description": "Keys to empty: what was recorded is wrong."},
                "summary_confirmed": {
                    "type": "boolean",
                    "description": "The person said yes to the requests you read back (review_requests): "
                    "give requests for the ones they confirmed.",
                },
                "requests": _REQUEST_IDS,
            }
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        desk = self.desk
        ids = _strings(args.get("requests")) or None
        answers = _mapping(args.get("answers"))
        inferred = _mapping(args.get("inferred"))
        confirmed, skipped, cleared = (
            _strings(args.get(each)) for each in ("confirmed", "skipped", "cleared")
        )

        async def answer() -> dict[str, Any] | None:
            requests = await desk.load(tool_context, ids=ids, status="open", here=ids is None)
            if not requests:
                return None
            recorded: dict[str, list[str]] = {}
            refused: dict[str, str] = {}
            taken: set[str] = set()
            for request in requests:
                engine = desk.engine(request)
                design = engine.design
                applied = apply_answers(
                    design,
                    request.record,
                    answers=answers,
                    inferred=inferred,
                    confirmed=confirmed,
                    skipped=skipped,
                    cleared=cleared,
                    others=True,
                )
                if applied.recorded:
                    recorded[request.title] = applied.recorded
                refused.update({f"{key} ({request.title})": why for key, why in applied.refused.items()})
                taken.update(field.key for field in design.fields)
                if args.get("summary_confirmed") is True and design.confirm_summary:
                    # Their yes is to what was read back: not to what changed since, nor to a request not ready.
                    if applied.changed:
                        refused[f"summary_confirmed ({request.title})"] = (
                            "it changed: read it back again (review_requests)"
                        )
                    elif not progress_of(design, request.record).ready:
                        refused[f"summary_confirmed ({request.title})"] = engine.not_done(request.record)
                    else:
                        request.record.summary_confirmed = True
                        recorded.setdefault(request.title, []).append("summary_confirmed")
            for key in [*answers, *inferred, *confirmed, *skipped, *cleared]:
                if key not in taken:
                    refused[key] = "no open request asks for it"
            await desk.save(tool_context, requests)
            out: dict[str, Any] = {"recorded": recorded}
            if refused:
                out["refused"] = refused
            return out

        out = await desk.retrying(tool_context, answer)
        if out is None:
            return {"error": "There's no open request to answer: start one (start_requests)."}
        return {**out, **await desk.refresh(tool_context)}


class ReviewRequests(_DeskTool):
    def __init__(self, desk: RequestDesk) -> None:
        super().__init__(
            desk,
            name="review_requests",
            description=(
                "Each open request as it stands, to read back to the person before they submit it: its values "
                "by label, what's still needed and what waits for their yes."
            ),
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return self._declare({"requests": _REQUEST_IDS})

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        desk = self.desk
        ids = _strings(args.get("requests")) or None
        out: list[dict[str, Any]] = []
        for request in await desk.load(tool_context, ids=ids, status="open", here=ids is None):
            engine = desk.engine(request)
            progress = progress_of(engine.design, request.record)
            values = engine.result(request.record, "completed")["record"]
            labels = {field.key: field.title for field in engine.design.fields}
            out.append(
                {
                    "id": request.id,
                    "intake": request.title,
                    "values": {labels.get(key, key): value for key, value in values.items()},
                    "still_needed": [field.title for field in progress.missing],
                    "waiting_for_yes": [field.title for field in progress.unconfirmed],
                    "ready": progress.ready,
                    **(
                        {"summary_confirmed": request.record.summary_confirmed}
                        if engine.design.confirm_summary
                        else {}
                    ),
                }
            )
        return {"requests": out}


class SubmitRequests(_DeskTool):
    def __init__(self, desk: RequestDesk) -> None:
        super().__init__(
            desk,
            name="submit_requests",
            description=(
                "Submits the person's ready requests, once they said yes to what you read back (recorded with "
                "answer_requests' summary_confirmed): each starts the workflows of its intake, whose runs the "
                "chat shows. A request that isn't ready, or whose summary waits for their yes, isn't submitted, "
                "and says what it still needs."
            ),
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return self._declare({"requests": _REQUEST_IDS})

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        desk = self.desk
        ids = _strings(args.get("requests")) or None

        async def close() -> tuple[list[IntakeRequest], list[dict[str, Any]]]:
            """Each ready request from open to submitting (its runs written down), kept only as it was read."""
            not_ready: list[dict[str, Any]] = []
            changed: list[IntakeRequest] = []
            for request in await desk.load(tool_context, ids=ids, status="open", here=ids is None):
                engine = desk.engine(request)
                if not engine.done(request.record):
                    progress = progress_of(engine.design, request.record)
                    not_ready.append(
                        {
                            "id": request.id,
                            "intake": request.title,
                            "still_needed": [field.key for field in progress.missing],
                            "waiting_for_yes": [field.key for field in progress.unconfirmed],
                            **(
                                {
                                    "summary": "read it back (review_requests); when they say it's right, "
                                    "record summary_confirmed (answer_requests)"
                                }
                                if progress.ready
                                else {}
                            ),
                        }
                    )
                    continue
                request.record.status = "completed"
                if engine.completions and not request.record.workflows:
                    request.record.workflows = await engine.plan_workflows(
                        tool_context,
                        request.record,
                        call_prefix=f"request-{request.id}",
                        source=StartSource("request", request.id),
                    )
                started = any(entry.get("status") == STARTING for entry in request.record.workflows)
                request.status = "submitting" if started else "submitted"
                changed.append(request)
            if changed:
                await desk.save(tool_context, changed)
            return changed, not_ready

        changed, not_ready = await desk.retrying(tool_context, close)
        # Those an earlier call left submitting (it stopped as it started
        # their runs) are finished too: each run starts once, whoever starts it.
        closed = {request.id for request in changed}
        left = [
            request
            for request in await desk.load(tool_context, ids=ids, status="submitting", here=ids is None)
            if request.id not in closed
        ]
        submitted: list[dict[str, Any]] = []
        for request in [*changed, *left]:
            engine = desk.engine(request)
            if request.status == "submitting":
                # Kept as submitting: now its runs start, once each.
                request = await self._started(tool_context, engine, request)
            result = engine.result(request.record, "completed")
            submitted.append(
                {
                    "id": request.id,
                    "intake": request.title,
                    "record": result["record"],
                    "workflows": result["workflows"],
                }
            )
        out: dict[str, Any] = {"submitted": submitted}
        if not_ready:
            out["not_ready"] = not_ready
        if ids:
            # Asked for by ID, and submitted already (another call, another tab): not again.
            done = closed | {request.id for request in left} | {entry["id"] for entry in not_ready}
            earlier = [
                {"id": request.id, "intake": request.title, "status": request.status}
                for request in await desk.load(tool_context, ids=[i for i in ids if i not in done])
                if request.status == "submitted"
            ]
            if earlier:
                out["already_submitted"] = earlier
        return {**out, **await desk.refresh(tool_context)}

    async def _started(
        self, context: ToolContext, engine: IntakeEngine, request: IntakeRequest
    ) -> IntakeRequest:
        """:return: The request once its runs started: submitted, each run where it got to."""
        answers = await engine.dispatch_workflows(request.record.workflows)

        async def keep() -> IntakeRequest:
            [current] = await self.desk.load(context, ids=[request.id]) or [request]
            if current.status != "submitting":
                return current
            current.record.workflows = engine.with_answers(current.record.workflows, answers)
            if not any(entry.get("status") == STARTING for entry in current.record.workflows):
                current.status = "submitted"
            await self.desk.save(context, [current])
            return current

        return await self.desk.retrying(context, keep)


class CancelRequests(_DeskTool):
    def __init__(self, desk: RequestDesk) -> None:
        super().__init__(
            desk,
            name="cancel_requests",
            description="Cancels open requests the person no longer wants, by ID.",
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return self._declare(
            {"requests": {**_REQUEST_IDS, "description": "Which requests, by ID."}}, ["requests"]
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        desk = self.desk

        async def cancel() -> list[IntakeRequest]:
            requests = await desk.load(tool_context, ids=_strings(args.get("requests")), status="open")
            for request in requests:
                request.status = "cancelled"
                request.record.status = "cancelled"
            await desk.save(tool_context, requests)
            return requests

        requests = await desk.retrying(tool_context, cancel)
        return {
            "cancelled": [{"id": request.id, "intake": request.title} for request in requests],
            **await desk.refresh(tool_context),
        }


class ListRequests(_DeskTool):
    def __init__(self, desk: RequestDesk) -> None:
        super().__init__(
            desk,
            name="list_requests",
            description=(
                "The person's requests with you, in this conversation and earlier ones, newest first: open ones "
                "(which start_requests can resume here) and submitted ones, with the runs they started."
            ),
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return self._declare(
            {
                "status": {
                    "type": "string",
                    "enum": ["open", "submitting", "submitted", "cancelled"],
                    "description": "Only those with this status; leave it out for any.",
                }
            }
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        desk = self.desk
        status = (
            args.get("status")
            if args.get("status") in ("open", "submitting", "submitted", "cancelled")
            else None
        )
        out: list[dict[str, Any]] = []
        for request in (await desk.load(tool_context, status=status))[:LISTED]:
            engine = desk.engine(request)
            entry: dict[str, Any] = {
                "id": request.id,
                "intake": request.title,
                "status": request.status,
                "updated_at": request.updated_at,
                "in_this_conversation": request.session_id == tool_context.session.id,
            }
            if request.status == "open":
                entry["still_needed"] = [
                    field.key for field in progress_of(engine.design, request.record).missing
                ]
            if request.record.workflows:
                entry["workflows"] = [
                    {key: run[key] for key in ("workflow", "run_id", "status") if key in run}
                    for run in request.record.workflows
                ]
            out.append(entry)
        return {"requests": out}


def request_tools(desk: RequestDesk) -> list[BaseTool]:
    """:return: The tools an agent files requests to its intakes with."""
    return [
        StartRequests(desk),
        AnswerRequests(desk),
        ReviewRequests(desk),
        SubmitRequests(desk),
        CancelRequests(desk),
        ListRequests(desk),
    ]
