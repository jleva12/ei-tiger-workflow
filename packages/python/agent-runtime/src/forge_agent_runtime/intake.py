"""An intake agent: an ADK task agent (``mode="task"``) that collects a
designed record from the person, in whatever order they give it.

What's designed (the node's settings) is the record: its fields, each with a
type, whether it's required, when it applies (a JSONata condition on the
record), whether the model may infer it, whether the person must confirm it
and whether the agent handing off may fill it in; and its sections, which
group the fields asked together and unlock on a condition of their own.

The record is kept in the session's state (:func:`state_key`), not in the
conversation: what the model is told before each call (each field, what's in,
what to ask next) is worked out from it, so the order is the person's and the
history can be compacted. The model does two things: it records what the
person says with ``record_answers``, any field at any time, and it words the
questions. Code decides what's missing and when it's done:

- ``finish_task`` (ADK's, which closes a task) carries the state's record,
  put into its call before the call is made, whatever the model typed; and
  it's refused while a required field is missing, a value waits for the
  person's yes or, when the summary must be confirmed, the summary does.
- Statuses besides ``completed`` hand back early: ``cancelled`` and
  ``escalated`` (when the design allows them), and ``interrupted``, when the
  person wants something the intake isn't for. An interrupted record carries
  on where it was when the agent is handed off to again.

An agent hands off to it by calling it, as ADK delegates a task: with the
fields the design lets it fill in and a background, and it gets back
``{status, note, record, missing, workflows}`` (:meth:`IntakeEngine.result`).
The person's messages go to the intake until it finishes.

What its ``on_complete`` way out leads to (the organization's workflows) is
started when it completes, by code, not the model: each with the record's
values its start takes, exactly once. As the ``finish_task`` call is made,
the runs are only written down (the runner's outbox, :meth:`WorkflowRunner.intend`)
and the record says so (``workflows``, each ``starting``), kept with the
model's call; once that's kept, as ``finish_task`` runs, they're started
(:meth:`WorkflowRunner.dispatch`). Each run's ID is made from the record's
ID and the workflow, never the model's call: a call made again (the turn
stopped, the tab closed) finds the runs the first wrote down, and a start
written down but never started is started by the runner's upkeep. Where
each run got to comes back in ``workflows``, with the call ID a chat
follows it by.

The record changes only as it was read (compare-and-set on its
``revision``): every change reads it, changes it and keeps it in one step,
or is refused (:class:`RecordConflict`) and made again.
"""

from __future__ import annotations

import datetime
import functools
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from forge_agent_runtime.services import INTAKE, StartSource, WorkflowStart, starting_workflows
from forge_common.adk.follow_ups import QUIET_KEY
from forge_common.patterns import BoundedPattern, compiled
from forge_jsonata import JException, Jsonata
from forge_jsonata.regex_engine import RegexFlags

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.tools.tool_context import ToolContext

    from forge_agent_runtime.services import WorkflowRunner

log = logging.getLogger(__name__)

#: The node kind.
KIND = "intake_agent"

#: Its way out to the workflows it starts when it completes.
ON_COMPLETE = "on_complete"

#: Each intake's record is kept in the state under this and its ADK name.
STATE_PREFIX = "forge:intake:"

#: The tool the model records what the person says with.
RECORD_TOOL = "record_answers"

#: ADK's tool that closes a task (``FinishTaskTool``).
FINISH_TOOL = "finish_task"

#: What an agent handing off may say besides the fields, as a call's argument.
BACKGROUND = "background"

#: A workflow run written down, not started yet (``workflows``' ``status``).
STARTING = "starting"

#: A field's key: what the record and the tools call it.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: How long a condition may take to evaluate, in milliseconds.
CONDITION_MS = 500
#: How long all of a record's conditions may take together, in milliseconds,
#: each time where it stands is worked out: the callbacks run on the event loop.
CONDITIONS_MS = 1_000

#: What it reads of the conversation before it was handed off to: the person's last messages, each cut to this.
SAID_MESSAGES = 6
SAID_CHARS = 1_000

#: The most fields and sections an intake may have: each condition is evaluated before every model call.
MOST_FIELDS = 200
MOST_SECTIONS = 50

FieldType = Literal["text", "number", "integer", "boolean", "date", "email", "choice", "choices"]
#: How a value came to be in the record.
How = Literal["stated", "inferred", "handoff", "confirmed", "skipped"]
#: How an intake can end.
Status = Literal["completed", "cancelled", "escalated", "interrupted"]

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRUE = {"true", "yes", "y"}
_FALSE = {"false", "no", "n"}


# --------------------------------------------------------------------------- #
# The design                                                                  #
# --------------------------------------------------------------------------- #


class IntakeField(BaseModel):
    """One field of the record."""

    model_config = ConfigDict(extra="ignore")

    #: What the record and the tools call it: lower-case letters, digits and _.
    key: str
    #: What people call it; the key when empty.
    label: str = ""
    type: FieldType = "text"
    #: A choice's options.
    options: list[str] = Field(default_factory=list)
    #: What it is, and how to ask for it.
    description: str = ""
    #: Why it's asked: what the agent says when the person asks.
    why: str = ""
    #: The record isn't done without it (when it applies).
    required: bool = True
    #: A JSONata condition on the record's values: it applies only when this holds. Empty: always.
    when: str = ""
    #: The model may work it out from what the person said; the person confirms it.
    infer: bool = False
    #: The person confirms its value before the intake finishes.
    confirm: bool = False
    #: The agent handing off may fill it in.
    prefill: bool = False
    #: Its section's ID; empty for none.
    section: str = ""

    @property
    def title(self) -> str:
        return self.label.strip() or self.key


class IntakeSection(BaseModel):
    """Fields asked together, unlocked on a condition."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    #: A JSONata condition on the record's values: its fields apply only when this holds.
    when: str = ""


class IntakeDesign(BaseModel):
    """An intake node's design: its record and how it may end."""

    model_config = ConfigDict(extra="ignore")

    #: The node's name, for what the model is told.
    name: str = "Intake"
    fields: list[IntakeField] = Field(default_factory=list)
    sections: list[IntakeSection] = Field(default_factory=list)
    #: It reads back a summary and waits for the person's yes before it finishes.
    confirm_summary: bool = True
    #: The person may stop it, and it hands back as cancelled.
    allow_cancel: bool = True
    #: It may hand back as escalated: for a person, or what it can't record.
    allow_escalate: bool = False
    #: It can ask with a form (adk_request_input).
    ask_person: bool = False
    #: It can show rich answers (present): it reads the summary back as a table.
    presents: bool = False
    #: Its rich answers have controls: it may ask with a form of them too.
    present_forms: bool = False

    @classmethod
    def of(cls, name: str, config: Mapping[str, Any]) -> IntakeDesign:
        return cls.model_validate({**config, "name": name})

    def field(self, key: str) -> IntakeField | None:
        return next((field for field in self.fields if field.key == key), None)

    @property
    def statuses(self) -> list[Status]:
        """How it may end."""
        out: list[Status] = ["completed"]
        if self.allow_cancel:
            out.append("cancelled")
        if self.allow_escalate:
            out.append("escalated")
        out.append("interrupted")
        return out

    def groups(self) -> list[tuple[IntakeSection | None, list[IntakeField]]]:
        """The fields by section, in the design's order: those in none first."""
        known = {section.id for section in self.sections}
        loose = [field for field in self.fields if field.section not in known]
        out: list[tuple[IntakeSection | None, list[IntakeField]]] = [(None, loose)] if loose else []
        for section in self.sections:
            out.append((section, [field for field in self.fields if field.section == section.id]))
        return out


def design_problems(name: str, config: Mapping[str, Any]) -> list[str]:
    """:return: What stops an intake node's design working; empty when nothing does."""
    try:
        design = IntakeDesign.of(name, config)
    except ValidationError as exc:
        return [f"{name!r}'s design: {exc.errors()[0]['msg']}."]
    out: list[str] = []
    if not design.fields:
        out.append(f"{name!r} has no fields: add what it collects.")
    if len(design.fields) > MOST_FIELDS or len(design.sections) > MOST_SECTIONS:
        return [f"{name!r} has more than {MOST_FIELDS} fields or {MOST_SECTIONS} sections."]
    keys: set[str] = set()
    for field in design.fields:
        if not KEY_PATTERN.match(field.key):
            out.append(
                f"{name!r}'s field {field.key!r}: a key is lower-case letters, digits and _, starting with a letter."
            )
        elif field.key == BACKGROUND:
            out.append(
                f"{name!r}'s field {field.key!r}: that key is kept for what the agent handing off says."
            )
        elif field.key in keys:
            out.append(f"{name!r} has two fields called {field.key!r}.")
        keys.add(field.key)
        options = [option.strip() for option in field.options]
        if field.type in ("choice", "choices"):
            if not any(options):
                out.append(f"{name!r}'s field {field.key!r} is a choice with no options.")
            elif len(set(options)) != len(options) or not all(options):
                out.append(f"{name!r}'s field {field.key!r} has an empty or repeated option.")
        if field.section and field.section not in {section.id for section in design.sections}:
            out.append(f"{name!r}'s field {field.key!r} is in a section it doesn't have.")
        out.extend(_condition_problem(name, f"field {field.key!r}", field.when))
    ids: set[str] = set()
    for section in design.sections:
        if section.id in ids:
            out.append(f"{name!r} has two sections with the ID {section.id!r}.")
        ids.add(section.id)
        out.extend(_condition_problem(name, f"section {section.title or section.id!r}", section.when))
    return out


def _condition_problem(name: str, what: str, condition: str) -> list[str]:
    if not condition.strip():
        return []
    try:
        Jsonata(condition, _bounded_patterns, timeout=CONDITION_MS)
    except (JException, ValueError, RecursionError) as exc:
        return [f"{name!r}'s {what}: its condition isn't JSONata: {exc}"]
    return []


def _bounded_patterns(pattern: str, flags: RegexFlags) -> BoundedPattern:
    """A condition's ``/.../`` on the bounded engine: Python's ``re`` would hold the event loop while it backtracks."""
    return compiled(
        pattern,
        seconds=CONDITION_MS / 1000,
        ignore_case=flags.case_insensitive,
        multiline=flags.multiline,
    )


def _evaluated(condition: str, data: Any, timeout_ms: int) -> bool:
    try:
        result = Jsonata(condition, _bounded_patterns, timeout=timeout_ms).evaluate(data)
    except (JException, ValueError, TypeError, ArithmeticError, RecursionError):
        return False
    return result is True


@functools.lru_cache(maxsize=256)
def _outcomes(conditions: tuple[str, ...], values: str) -> tuple[bool, ...]:
    """
    Whether each condition holds on a record's values (as JSON), all of them
    in ``CONDITIONS_MS``: once that's spent, those left don't hold. Kept for
    the same values, so the callbacks of one model step work them out once.
    """
    data = json.loads(values)
    deadline = time.monotonic() + CONDITIONS_MS / 1000
    out: list[bool] = []
    for condition in conditions:
        left = int((deadline - time.monotonic()) * 1000)
        if left <= 0:
            log.warning("An intake's conditions took over %d ms: those left don't hold", CONDITIONS_MS)
            out.extend([False] * (len(conditions) - len(out)))
            break
        out.append(_evaluated(condition, data, min(CONDITION_MS, left)))
    return tuple(out)


def _holding(conditions: Sequence[str], values: Mapping[str, Any]) -> dict[str, bool]:
    """:return: Whether each condition holds on the values: an empty one always does, a failing one doesn't."""
    asked = tuple(dict.fromkeys(condition for condition in conditions if condition.strip()))
    try:
        text = json.dumps(dict(values), sort_keys=True)
    except (TypeError, ValueError):
        found: tuple[bool, ...] = tuple(False for _ in asked)
    else:
        found = _outcomes(asked, text) if asked else ()
    return {**dict(zip(asked, found, strict=True)), **{c: True for c in conditions if not c.strip()}}


def holds(condition: str, values: Mapping[str, Any]) -> bool:
    """
    :return: Whether a condition holds on the record's values: only true
        holds, as a workflow's conditions; an empty one always does, a
        failing one (or one that takes longer than ``CONDITION_MS``) doesn't.
    """
    return _holding([condition], values)[condition]


# --------------------------------------------------------------------------- #
# Values                                                                      #
# --------------------------------------------------------------------------- #


class ValueRefused(ValueError):
    """A value a field can't take, and why."""


def checked(field: IntakeField, value: Any) -> Any:
    """
    :return: ``value`` as ``field`` keeps it: numbers and yes or no from
        their words, a choice as its option is written.
    :raises ValueRefused: It isn't one the field takes.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueRefused("it's empty: to leave it empty, put it in cleared")
    match field.type:
        case "text":
            if isinstance(value, (dict, list)):
                raise ValueRefused("it's text")
            return str(value).strip()
        case "number" | "integer":
            number = _number(value)
            if field.type == "integer" and not number.is_integer():
                raise ValueRefused("it's a whole number")
            return int(number) if number.is_integer() else number
        case "boolean":
            if isinstance(value, bool):
                return value
            word = str(value).strip().lower()
            if word in _TRUE:
                return True
            if word in _FALSE:
                return False
            raise ValueRefused("it's yes or no (true or false)")
        case "date":
            text = str(value).strip()
            try:
                return datetime.date.fromisoformat(text).isoformat()
            except ValueError:
                raise ValueRefused("it's a date, as YYYY-MM-DD") from None
        case "email":
            text = str(value).strip()
            if not EMAIL.match(text):
                raise ValueRefused("it's an email address")
            return text
        case "choice":
            return _option(field, value)
        case "choices":
            items = value if isinstance(value, list) else [value]
            picked: list[str] = []
            for item in items:
                option = _option(field, item)
                if option not in picked:
                    picked.append(option)
            if not picked:
                raise ValueRefused("it's one or more of its options")
            return picked
        case _:
            raise ValueRefused(f"its type {field.type!r} isn't one Forge knows")


def _number(value: Any) -> float:
    """:return: A number, or its words ("1,200"), as a float."""
    if isinstance(value, (bool, dict, list)):
        raise ValueRefused("it's a number")
    try:
        number = float(str(value).replace(",", "").strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValueRefused("it's a number") from None
    if not math.isfinite(number):
        raise ValueRefused("it's a number")
    return number


def _option(field: IntakeField, value: Any) -> str:
    text = str(value).strip().lower()
    for option in field.options:
        if option.strip().lower() == text:
            return option.strip()
    raise ValueRefused(f"it's one of {', '.join(json.dumps(o) for o in field.options)}")


def value_schema(field: IntakeField) -> dict[str, Any]:
    """:return: A JSON Schema of a field's value, for what the model is offered."""
    about = " ".join(part for part in (field.title + ".", field.description.strip()) if part)
    match field.type:
        case "number":
            schema: dict[str, Any] = {"type": "number"}
        case "integer":
            schema = {"type": "integer"}
        case "boolean":
            schema = {"type": "boolean"}
        case "date":
            schema = {"type": "string"}
            about += " A date, as YYYY-MM-DD."
        case "email":
            schema = {"type": "string"}
            about += " An email address."
        case "choice":
            schema = {"type": "string", "enum": [option.strip() for option in field.options]}
        case "choices":
            schema = {
                "type": "array",
                "items": {"type": "string", "enum": [o.strip() for o in field.options]},
            }
        case _:
            schema = {"type": "string"}
    return {**schema, "description": about}


def _python_type(field: IntakeField) -> Any:
    """A field's value as a Python type, for the call an agent hands off with."""
    match field.type:
        case "number":
            return float
        case "integer":
            return int
        case "boolean":
            return bool
        case "choice":
            return Literal[tuple(option.strip() for option in field.options)]  # type: ignore[misc]
        case "choices":
            return list[Literal[tuple(option.strip() for option in field.options)]]  # type: ignore[misc]
        case _:
            return str


# --------------------------------------------------------------------------- #
# The record                                                                  #
# --------------------------------------------------------------------------- #


class Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: Any = None
    how: How = "stated"


class IntakeRecord(BaseModel):
    """What an intake has collected, kept in the state."""

    model_config = ConfigDict(extra="ignore")

    #: Made from the conversation and the task it was started in: what the
    #: runs it starts are known by.
    id: str = ""
    #: Counts its changes: each is kept only on the revision it was read at.
    revision: int = 0
    #: The task it's collected in: the hand-off's call ID (its isolation scope).
    scope: str | None = None
    status: Literal["open", "completed", "cancelled", "escalated", "interrupted"] = "open"
    answers: dict[str, Answer] = Field(default_factory=dict)
    #: What the agent that handed off said.
    background: str = ""
    #: What the person said before it was handed off to: their last messages in the
    #: conversation it was handed from (a task sees only its own).
    said: list[str] = Field(default_factory=list)
    #: The workflows it started when it completed: where each run got to.
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    #: The turn (invocation) its runs were started in: the agent it hands
    #: back to is told where they got to then (:class:`IntakeRunsPlugin`).
    started_in: str | None = None
    #: The person said yes to the summary read back to them, and nothing's changed since.
    summary_confirmed: bool = False

    @property
    def values(self) -> dict[str, Any]:
        """The values conditions read: every answer's but skipped ones."""
        return {key: answer.value for key, answer in self.answers.items() if answer.how != "skipped"}


def state_key(agent: str) -> str:
    """:return: Where an intake's record is kept in the state, by its ADK name."""
    return STATE_PREFIX + agent


def record_id(session_id: str, agent: str, scope: str | None) -> str:
    """
    :return: A record's ID, made from the conversation, the intake and the
        task it's started in: the same however often it's made again (a
        turn whose state wasn't kept), so the runs it starts are too.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"forge:intake:{session_id}:{agent}:{scope or ''}").hex


class RecordConflict(Exception):
    """The record changed since it was read: read it again."""


def latest(context: CallbackContext) -> Mapping[str, Any]:
    """
    :return: What a change reads: the state as the call's (or callback's)
        session has it now. A call's own ``state`` answers with what that
        call wrote, even after a call made beside it wrote since; the
        session's state is shared by the calls, and has every write.
    """
    return context.session.state


@dataclass(frozen=True)
class Progress:
    """Where a record stands against its design."""

    #: The fields that apply now, in the design's order.
    applying: list[IntakeField]
    #: Required fields that apply and have no value.
    missing: list[IntakeField]
    #: Fields with a value that waits for the person's yes.
    unconfirmed: list[IntakeField]
    #: Optional fields that apply, unanswered and not skipped.
    optional: list[IntakeField]
    #: Sections whose condition doesn't hold.
    locked: list[IntakeSection]
    #: What to ask next: the first section with something still to ask.
    ask_next: list[IntakeField]
    #: The section ``ask_next`` is in; None for the fields in none.
    next_section: IntakeSection | None

    @property
    def ready(self) -> bool:
        """Every required field is in and every value confirmed that must be."""
        return not self.missing and not self.unconfirmed


def needs_yes(field: IntakeField, answer: Answer) -> bool:
    """:return: Whether a value waits for the person's yes: inferred, or of a field they confirm."""
    if answer.how == "skipped" or answer.how == "confirmed":
        return False
    return answer.how == "inferred" or field.confirm


def progress_of(design: IntakeDesign, record: IntakeRecord) -> Progress:
    """
    :return: Where the record stands. What to ask next is the first section
        (in the design's order) with a required field still empty, with its
        optional ones; once every required field is in, the first with an
        optional one unanswered.
    """
    when = [section.when for section in design.sections] + [field.when for field in design.fields]
    holding = _holding(when, record.values)
    applying: list[IntakeField] = []
    unconfirmed: list[IntakeField] = []
    locked: list[IntakeSection] = []
    # Each open section's empty fields: required, then optional.
    open_: list[tuple[IntakeSection | None, list[IntakeField], list[IntakeField]]] = []
    for section, fields in design.groups():
        if section is not None and not holding[section.when]:
            locked.append(section)
            continue
        due: list[IntakeField] = []
        maybe: list[IntakeField] = []
        for field in fields:
            if not holding[field.when]:
                continue
            applying.append(field)
            answer = record.answers.get(field.key)
            if answer is None:
                (due if field.required else maybe).append(field)
            elif needs_yes(field, answer):
                unconfirmed.append(field)
        open_.append((section, due, maybe))
    missing = [field for _, due, _ in open_ for field in due]
    optional = [field for _, _, maybe in open_ for field in maybe]
    first = next(((s, d, m) for s, d, m in open_ if d), None) or next(
        ((s, d, m) for s, d, m in open_ if m), None
    )
    next_section, ask_next = (first[0], [*first[1], *first[2]]) if first else (None, [])
    return Progress(applying, missing, unconfirmed, optional, locked, ask_next, next_section)


# --------------------------------------------------------------------------- #
# The engine                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Completion:
    """A workflow an intake starts when it completes (a Workflow node on its ``on_complete`` way)."""

    #: The workflow's name.
    name: str
    #: Which of the organization's workflows, at which version (``ag_x``, ``ag_x@3``, ``ag_x@draft``).
    ref: str
    #: Seconds to wait for its run before handing back where it got to.
    wait: float
    #: The input fields its start takes; None for any (the whole record).
    takes: frozenset[str] | None = None


class IntakeEngine:
    """
    An intake node's design, run: the tool the model records with, the
    callbacks that keep the record and tell the model where it stands, the
    call an agent hands off with and what it gets back.

    :param design: The node's design.
    :param agent: The agent's ADK name: its record's key in the state.
    """

    def __init__(
        self,
        design: IntakeDesign,
        agent: str,
        *,
        completions: Sequence[Completion] = (),
        runner: WorkflowRunner | None = None,
        organization_id: str | None = None,
    ) -> None:
        """
        :param completions: The workflows it starts when it completes.
        :param runner: What starts them (the hosted runtime's).
        :param organization_id: Whose workflows they are.
        """
        self.design = design
        self.agent = agent
        self.key = state_key(agent)
        self.completions = list(completions)
        self.runner = runner
        self.organization_id = organization_id

    # The record ---------------------------------------------------------------

    def load(self, state: Mapping[str, Any]) -> IntakeRecord:
        """:return: The record in ``state``; a new one when there's none."""
        raw = state.get(self.key)
        try:
            return IntakeRecord.model_validate(raw) if isinstance(raw, Mapping) else IntakeRecord()
        except ValidationError:
            return IntakeRecord()

    def read(self, context: CallbackContext) -> IntakeRecord:
        """:return: The record as the call's (or callback's) session has it now (:func:`latest`)."""
        return self.load(latest(context))

    def save(self, context: CallbackContext, record: IntakeRecord) -> None:
        """
        Keeps the record with the call (or callback), if it's still at the
        revision it was read at, as the session has it now: it's at the next one.

        :raises RecordConflict: It changed since it was read.
        """
        kept = latest(context).get(self.key)
        at = kept.get("revision", 0) if isinstance(kept, Mapping) else 0
        if at != record.revision:
            raise RecordConflict(f"{self.agent}'s record changed since it was read.")
        record.revision += 1
        context.state[self.key] = record.model_dump(mode="json")

    def result(self, record: IntakeRecord, status: Status, note: str = "") -> dict[str, Any]:
        """:return: What the agent that handed off gets back: the record's values that apply, and what's missing."""
        progress = progress_of(self.design, record)
        values = {
            field.key: record.answers[field.key].value
            for field in progress.applying
            if field.key in record.answers and record.answers[field.key].how != "skipped"
        }
        return {
            "status": status,
            "note": note,
            "record": values,
            "missing": [field.key for field in progress.missing],
            "workflows": list(record.workflows),
        }

    def done(self, record: IntakeRecord) -> bool:
        """Whether it may finish as completed."""
        return progress_of(self.design, record).ready and (
            record.summary_confirmed or not self.design.confirm_summary
        )

    # The hand-off -------------------------------------------------------------

    def input_schema(self) -> type[BaseModel]:
        """What an agent hands off with: the fields it may fill in, and a background."""
        fields: dict[str, Any] = {
            "background_": (
                str | None,
                Field(
                    default=None,
                    alias=BACKGROUND,
                    description="Everything the person told you that it could use, in their words (their "
                    "details, what happened, dates, numbers), and what they asked for: they aren't asked again.",
                ),
            )
        }
        for field in self.design.fields:
            if field.prefill and KEY_PATTERN.match(field.key):
                fields[f"{field.key}_"] = (
                    _python_type(field) | None,
                    Field(
                        default=None,
                        alias=field.key,
                        description=f"{value_schema(field)['description']} Only what the person already told you.",
                    ),
                )
        return create_model(  # type: ignore[call-overload,no-any-return]
            "IntakeInput", __config__=ConfigDict(extra="forbid", populate_by_name=True), **fields
        )

    def output_schema(self) -> type[BaseModel]:
        """What ``finish_task`` is called with: how it ends and a note; the record is put in for the model."""
        statuses = tuple(self.design.statuses)
        return create_model(  # type: ignore[call-overload,no-any-return]
            "IntakeResult",
            __config__=ConfigDict(extra="forbid"),
            status=(
                Literal[statuses],  # type: ignore[valid-type]
                Field(description="How the intake ends."),
            ),
            note=(str, Field(default="", description="Anything the agent you hand back to should know.")),
            record=(
                dict[str, Any],
                Field(
                    default_factory=dict, description="Filled in for you from what's recorded: leave it out."
                ),
            ),
            missing=(
                list[str],
                Field(default_factory=list, description="Filled in for you: leave it out."),
            ),
            workflows=(
                list[dict[str, Any]],
                Field(default_factory=list, description="Filled in for you: leave it out."),
            ),
        )

    def description(self, said: str) -> str:
        """What the agent handing off reads: what it's for, and what it collects."""
        collects = ", ".join(field.title for field in self.design.fields[:12])
        more = len(self.design.fields) - 12
        if more > 0:
            collects += f" and {more} more"
        what = said.strip() or f"Runs the intake {self.design.name}: it asks the person for what it needs."
        starts = ""
        if self.completions:
            names = ", ".join(completion.name for completion in self.completions)
            starts = (
                f" When it completes it starts {names} with the record: each run (its run_id) comes "
                "back in workflows, and you're told where it got to (its status and result) right after."
            )
        return (
            f"{what} It collects: {collects}. Hand off to it with what the person already told you; "
            f"it talks with them until it's done, then hands back the record.{starts}"
        )

    # Callbacks ----------------------------------------------------------------

    def before_model(self, callback_context: CallbackContext, llm_request: LlmRequest) -> None:
        """Before each model call: the record for this task, and where it stands, in the instruction."""
        scope = callback_context.isolation_scope
        record = self.read(callback_context)
        if record.scope != scope:
            # Handed off to again: an open or interrupted record carries on, a finished one starts over.
            if record.status in ("open", "interrupted"):
                carried = record
            else:
                carried = IntakeRecord(
                    id=record_id(callback_context.session.id, self.agent, scope), revision=record.revision
                )
            record = carried.model_copy(update={"scope": scope, "status": "open"})
            record.id = record.id or record_id(callback_context.session.id, self.agent, scope)
            self._prefill(record, self._handed(callback_context, scope))
            record.said = self._said(callback_context, scope)
            self.save(callback_context, record)
        llm_request.append_instructions([self.brief(record)])

    def _handed(self, callback_context: CallbackContext, scope: str | None) -> Mapping[str, Any]:
        """:return: What the hand-off's call said: its arguments, found by its ID (the task's scope)."""
        if not scope:
            return {}
        for event in reversed(callback_context.session.events):
            for call in event.get_function_calls():
                if call.id == scope:
                    return dict(call.args or {})
        return {}

    def _said(self, callback_context: CallbackContext, scope: str | None) -> list[str]:
        """
        :return: The person's last messages before the hand-off, in the
            conversation it was handed from: a task's history starts at its
            hand-off, and what they said there is what the record starts from.
        """
        said: list[str] = []
        for event in callback_context.session.events:
            if scope and any(call.id == scope for call in event.get_function_calls()):
                break
            if event.author != "user" or event.isolation_scope or not event.content:
                continue
            text = " ".join(part.text for part in event.content.parts or () if part.text and not part.thought)
            if text.strip():
                said.append(text.strip()[:SAID_CHARS])
        return said[-SAID_MESSAGES:]

    def _prefill(self, record: IntakeRecord, args: Mapping[str, Any]) -> None:
        background = args.get(BACKGROUND)
        if isinstance(background, str) and background.strip():
            record.background = background.strip()
        for field in self.design.fields:
            value = args.get(field.key)
            if not field.prefill or value is None:
                continue
            current = record.answers.get(field.key)
            # What the person said themselves stays.
            if current is not None and current.how != "handoff":
                continue
            try:
                record.answers[field.key] = Answer(value=checked(field, value), how="handoff")
            except ValueRefused:
                continue

    async def after_model(self, callback_context: CallbackContext, llm_response: LlmResponse) -> None:
        """
        After each model call: ``finish_task``'s call carries the record as it
        is, not as the model typed it; made beside other calls, it waits
        until they're done (it's dropped, and the model calls it again). A
        call that completes it writes its workflows' runs down (not started
        yet: that's once the call is kept, as ``finish_task`` runs).
        """
        content = llm_response.content
        parts = list(content.parts or []) if content else []
        finishing = [p for p in parts if p.function_call and p.function_call.name == FINISH_TOOL]
        if finishing:
            others = [p for p in parts if p.function_call and p.function_call.name != FINISH_TOOL]
            if others and content is not None:
                content.parts = [p for p in parts if p not in finishing]
            else:
                record = self.read(callback_context)
                for part in finishing:
                    call = part.function_call
                    assert call is not None
                    args = dict(call.args or {})
                    # A status it can't end as stays, for before_tool to refuse saying which it can.
                    asked = args.get("status")
                    status = cast("Status", asked) if isinstance(asked, str) else "completed"
                    if (
                        status == "completed"
                        and self.done(record)
                        and self.completions
                        and not record.workflows
                    ):
                        record.id = record.id or record_id(
                            callback_context.session.id, self.agent, record.scope
                        )
                        # Kept with this call (the model's event): only then are they started.
                        record.workflows = await self.plan_workflows(
                            callback_context,
                            record,
                            call_prefix=f"intake-{record.id}",
                            source=StartSource("record", record.id, state_key=self.key),
                        )
                        self.save(callback_context, record)
                    call.args = self.result(record, status, str(args.get("note") or ""))
        # Its questions aren't replies to suggest follow-ups after.
        llm_response.custom_metadata = {**(llm_response.custom_metadata or {}), QUIET_KEY: True}

    async def plan_workflows(
        self,
        context: CallbackContext,
        record: IntakeRecord,
        *,
        call_prefix: str,
        source: StartSource,
    ) -> list[dict[str, Any]]:
        """
        Writes down the runs of its workflows, each with the record's values
        its start takes (:meth:`WorkflowRunner.intend`): started once the
        change completing the record is kept (:meth:`dispatch_workflows`).

        :param call_prefix: What each run's call ID starts with: the
            record's or request's, so it's the same however often it's asked.
        :return: Each run as the record keeps it: ``starting``, with its ID
            and the call ID a chat follows it by; or why it can't start (the
            intake completes all the same).
        """
        values = self.result(record, "completed")["record"]
        if self.runner is None:
            return [
                {"workflow": completion.name, "error": "Workflows run only in the hosted runtime."}
                for completion in self.completions
            ]
        starts = [
            WorkflowStart(
                call_id=f"{call_prefix}-{index}",
                ref=completion.ref,
                name=completion.name,
                input=values
                if completion.takes is None
                else {k: v for k, v in values.items() if k in completion.takes},
            )
            for index, completion in enumerate(self.completions)
        ]
        try:
            # Started by the intake completing, which the run records.
            with starting_workflows(INTAKE):
                run_ids = await self.runner.intend(
                    starts, organization_id=self.organization_id, context=context, source=source
                )
        except Exception as error:  # noqa: BLE001 - runs that can't start don't fail the person's turn
            log.warning("Intake %s couldn't write down its workflows' runs", self.agent, exc_info=True)
            return [{"workflow": start.name, "error": str(error) or type(error).__name__} for start in starts]
        return [
            {"workflow": start.name, "call_id": start.call_id, "run_id": run_id, "status": STARTING}
            for start, run_id in zip(starts, run_ids, strict=True)
        ]

    async def dispatch_workflows(self, workflows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        """
        Starts the runs written down and not started yet (once each: a run
        already started is the run), each waited for as its Workflow node says.

        :return: Where each got to, by call ID.
        """
        out: dict[str, dict[str, Any]] = {}
        if self.runner is None:
            return out
        for index, entry in enumerate(workflows):
            if entry.get("status") != STARTING or not entry.get("run_id"):
                continue
            wait = self.completions[index].wait if index < len(self.completions) else 0.0
            try:
                answer = await self.runner.dispatch(
                    str(entry["run_id"]), organization_id=self.organization_id, wait=wait
                )
            except Exception as error:  # noqa: BLE001 - its run is started later by the runner's upkeep
                log.warning("Intake %s couldn't start run %s", self.agent, entry.get("run_id"), exc_info=True)
                out[str(entry["call_id"])] = {"note": f"Starting it failed for now ({error}); it's retried."}
                continue
            out[str(entry["call_id"])] = dict(answer)
        return out

    @staticmethod
    def with_answers(
        workflows: Sequence[Mapping[str, Any]], answers: Mapping[str, Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """:return: ``workflows``, each with where its run got to (``answers``, by call ID)."""
        return [
            {**entry, **answers.get(str(entry.get("call_id")), {})} if entry.get("call_id") else dict(entry)
            for entry in workflows
        ]

    async def before_tool(
        self, tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        """
        Before ``finish_task`` runs: refused when the record isn't done;
        otherwise the record is closed, and, completed, its workflows' runs
        are started (the call that wrote them down is kept by now). What
        it hands back is the call as it was kept (each run ``starting``);
        where they got to, the agent it hands back to is told before its
        next model call (:class:`IntakeRunsPlugin`).
        """
        if tool.name != FINISH_TOOL:
            return None
        record = self.read(tool_context)
        status = args.get("status")
        if status not in self.design.statuses:
            return {"error": f"It ends as one of: {', '.join(self.design.statuses)}."}
        if status == "completed" and not self.done(record):
            return {"error": self.not_done(record)}
        record.status = status
        self.save(tool_context, record)
        if status == "completed" and record.workflows:
            answers = await self.dispatch_workflows(record.workflows)
            for _ in range(3):
                current = self.read(tool_context)
                current.workflows = self.with_answers(current.workflows, answers)
                current.started_in = tool_context.invocation_id
                try:
                    self.save(tool_context, current)
                except RecordConflict:
                    continue
                break
        return None

    def not_done(self, record: IntakeRecord) -> str:
        progress = progress_of(self.design, record)
        parts: list[str] = []
        if progress.missing:
            parts.append("still needed: " + ", ".join(f.key for f in progress.missing))
        if progress.unconfirmed:
            parts.append("waiting for the person's yes: " + ", ".join(f.key for f in progress.unconfirmed))
        if not parts and self.design.confirm_summary and not record.summary_confirmed:
            parts.append(
                "read back a summary of what's recorded and ask if it's right; when they say yes, "
                "record summary_confirmed"
            )
        return "It isn't done: " + "; ".join(parts) + ". Carry on with the person."

    # What the model is told ----------------------------------------------------

    def brief(self, record: IntakeRecord) -> str:
        """The intake, where it stands and how to carry on: what the model is told before each call."""
        design = self.design
        progress = progress_of(design, record)
        lines = [
            f"# The intake: {design.name}",
            "",
            "You run this intake: you collect the record below from the person, in whatever order they "
            "give it. The record is kept for you, so trust it over the conversation.",
            "",
            "How to work:",
            f"- As soon as the person tells you anything a field asks for, record it with {RECORD_TOOL}: "
            "several fields at once, in any order, even ones you haven't asked about yet. Don't wait "
            "until the end. A correction is recorded the same way.",
            '- Ask for what\'s next ("Ask next" below), a few related things at a time, in plain words. '
            "Don't show them field keys. Acknowledge what they've already told you instead of asking again.",
            '- If they ask why something\'s needed, answer from its "why we ask".',
        ]
        if any(field.infer for field in design.fields):
            lines.append(
                f'- Fields marked "may infer" you may work out from what they said: record those under '
                f"inferred in {RECORD_TOOL}, and they'll need the person's yes."
            )
        lines.append(
            "- When they confirm a value is right, record it under confirmed. Optional fields: offer them; "
            "one they can't or won't answer goes under skipped."
        )
        if design.ask_person:
            lines.append(
                "- For a choice, a date or a yes or no you may ask with adk_request_input, so the chat shows a form."
            )
        if design.present_forms:
            lines.append(
                "- You may ask with present too: a Form (or a Card with asForm), each field named by the key it's "
                "for, with a reply _action and a confirm; one card per form. What they fill in comes back as "
                f"their next message, field by field: record it with {RECORD_TOOL}."
            )
        if design.confirm_summary:
            if design.presents:
                read_back = (
                    "show it with present (not a form): a Table of each field and its value, with Choices "
                    "for it being right and for changing something (their pick comes back as their "
                    "message), and a sentence saying it's there to check"
                )
            else:
                read_back = (
                    "each field and its value in plain words, and ask if it's right: the summary goes in "
                    "what they read (a form's message too), never just the question"
                )
            lines.append(
                f"- When nothing is still needed, read back a summary of everything recorded: {read_back}. "
                f"When they say yes, record summary_confirmed with {RECORD_TOOL}, then call {FINISH_TOOL} "
                'with status "completed".'
            )
        else:
            lines.append(f'- When nothing is still needed, call {FINISH_TOOL} with status "completed".')
        if design.allow_cancel:
            lines.append(f'- If they want to stop, call {FINISH_TOOL} with status "cancelled".')
        if design.allow_escalate:
            lines.append(
                f"- If they need a person, or something you can't record, call {FINISH_TOOL} with status "
                '"escalated" and a note saying why.'
            )
        lines.extend(
            [
                f"- If they ask for something this intake isn't for, call {FINISH_TOOL} with status "
                "\"interrupted\" and a note of what they asked; what they've given is kept for when you're "
                "handed this again.",
                f"- {FINISH_TOOL} takes the status and a note: the record is put in for you.",
            ]
        )
        if record.background:
            lines.extend(["", f"What the agent that handed off said: {record.background}"])
        if record.said:
            lines.extend(
                [
                    "",
                    "What the person said before you were handed this (you didn't see it, they did: record "
                    f"everything in it a field asks for with {RECORD_TOOL} before you ask them anything):",
                    *(f"- {message}" for message in record.said),
                ]
            )

        lines.extend(["", "## The record"])
        for section, fields in design.groups():
            if section is not None and section in progress.locked:
                when = section.when.strip()
                lines.append(f"\n### {section.title or section.id} (not asked yet: unlocks when {when})")
                continue
            if section is not None:
                lines.append(f"\n### {section.title or section.id}")
            for field in fields:
                lines.append(self._field_line(field, record, field in progress.applying))

        lines.append("")

        def named(fields: list[IntakeField]) -> str:
            return ", ".join(f"{f.title} ({f.key})" for f in fields)

        if progress.missing:
            where = (
                f" ({progress.next_section.title or progress.next_section.id})"
                if progress.next_section
                else ""
            )
            lines.append(f"Ask next{where}: {named(progress.ask_next)}")
        elif progress.optional and not record.summary_confirmed:
            # Offered once, before the summary; a person who'd rather not skips them.
            lines.append(
                f"Optional, not asked yet: {named(progress.optional)}. Offer them once before the summary."
            )
        if progress.unconfirmed:
            lines.append(
                "Waiting for the person's yes: "
                + ", ".join(
                    f"{f.title} ({f.key}) = {json.dumps(record.answers[f.key].value)}"
                    for f in progress.unconfirmed
                )
            )
        required = [f for f in progress.applying if f.required]
        lines.append(f"Required fields in: {len(required) - len(progress.missing)} of {len(required)}.")
        if progress.ready:
            if design.confirm_summary and not record.summary_confirmed:
                lines.append("Everything's in: read back the summary and ask if it's right.")
            else:
                lines.append(f'Everything\'s in and confirmed: call {FINISH_TOOL} with status "completed".')
        return "\n".join(lines)

    def _field_line(self, field: IntakeField, record: IntakeRecord, applies: bool) -> str:
        answer = record.answers.get(field.key)
        facts = [
            field.type
            if field.type not in ("choice", "choices")
            else f"{field.type}: {', '.join(field.options)}"
        ]
        facts.append("required" if field.required else "optional")
        if field.when.strip():
            facts.append(f"when {field.when.strip()}" + ("" if applies else ", doesn't apply now"))
        if field.infer:
            facts.append("may infer")
        if field.confirm:
            facts.append("the person confirms it")
        if answer is None:
            state = "empty"
        elif answer.how == "skipped":
            state = "skipped"
        else:
            how = {
                "stated": "the person said",
                "inferred": "you inferred",
                "handoff": "from the agent that handed off",
                "confirmed": "confirmed",
            }[answer.how]
            state = f"{json.dumps(answer.value, ensure_ascii=False)} ({how})"
        line = f"- {field.title} ({field.key}; {'; '.join(facts)}): {state}"
        if field.description.strip():
            line += f". {field.description.strip()}"
        if field.why.strip():
            line += f" Why we ask: {field.why.strip()}"
        return line

    def tool(self) -> RecordAnswersTool:
        return RecordAnswersTool(self)


class IntakeRunsPlugin(BasePlugin):
    """
    Tells an agent an intake handed back to where the runs it started got
    to, before its next model call in that turn: the intake hands back its
    runs as it wrote them down (``starting``), and starts them as it hands
    back (:meth:`IntakeEngine.before_tool`).
    """

    def __init__(self) -> None:
        super().__init__(name="forge_intake_runs")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        state = latest(callback_context)
        lines: list[str] = []
        for key in [key for key in state if key.startswith(STATE_PREFIX)]:
            raw = state[key]
            intake = key.removeprefix(STATE_PREFIX)
            if (
                not isinstance(raw, Mapping)
                or raw.get("started_in") != callback_context.invocation_id
                or callback_context.agent_name == intake
            ):
                continue
            for run in raw.get("workflows") or []:
                if isinstance(run, Mapping) and run.get("run_id"):
                    shown = {k: run[k] for k in ("status", "result", "error", "note") if k in run}
                    lines.append(
                        f"- {run.get('workflow')} (run {run['run_id']}, call {run.get('call_id')}): "
                        f"{json.dumps(shown, ensure_ascii=False, default=str)[:2000]}"
                    )
        if lines:
            llm_request.append_instructions(
                [
                    "The workflow runs an intake started as it handed back to you, and where each got to:",
                    *lines,
                ]
            )
        return None


class RecordAnswersTool(BaseTool):
    """Records what the person said into the intake's record: any fields, in any order."""

    def __init__(self, engine: IntakeEngine) -> None:
        super().__init__(
            name=RECORD_TOOL,
            description=(
                "Records what the person told you in the intake's record: any fields, in any order, "
                "several at once. Call it as soon as they say something a field asks for."
            ),
        )
        self.engine = engine

    def _get_declaration(self) -> types.FunctionDeclaration:
        design = self.engine.design
        keys = [field.key for field in design.fields]
        properties: dict[str, Any] = {
            "answers": {
                "type": "object",
                "description": "What the person said, by field key. A correction replaces what was there.",
                "properties": {field.key: value_schema(field) for field in design.fields},
                "additionalProperties": False,
            },
            "confirmed": {
                "type": "array",
                "items": {"type": "string", "enum": keys},
                "description": "Fields the person said are right, when you read them back.",
            },
            "cleared": {
                "type": "array",
                "items": {"type": "string", "enum": keys},
                "description": "Fields to empty: what was recorded is wrong and there's nothing to put instead.",
            },
        }
        inferable = [field for field in design.fields if field.infer]
        if inferable:
            properties["inferred"] = {
                "type": "object",
                "description": "What you worked out from what they said, but they didn't say outright.",
                "properties": {field.key: value_schema(field) for field in inferable},
                "additionalProperties": False,
            }
        optional = [field.key for field in design.fields if not field.required]
        if optional:
            properties["skipped"] = {
                "type": "array",
                "items": {"type": "string", "enum": optional},
                "description": "Optional fields the person can't or won't answer.",
            }
        if design.confirm_summary:
            properties["summary_confirmed"] = {
                "type": "boolean",
                "description": "The person said yes to the summary you read back.",
            }
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={"type": "object", "properties": properties},
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        engine = self.engine
        design = engine.design
        record = engine.read(tool_context)
        applied = apply_answers(
            design,
            record,
            answers=_mapping(args.get("answers")),
            inferred=_mapping(args.get("inferred")),
            confirmed=_keys(args.get("confirmed")),
            skipped=_keys(args.get("skipped")),
            cleared=_keys(args.get("cleared")),
        )
        refused = dict(applied.refused)
        if args.get("summary_confirmed") is True:
            progress = progress_of(design, record)
            if applied.changed:
                refused["summary_confirmed"] = "the record changed: read back the summary again"
            elif not progress.ready:
                refused["summary_confirmed"] = engine.not_done(record)
            else:
                record.summary_confirmed = True
        engine.save(tool_context, record)

        progress = progress_of(design, record)
        out: dict[str, Any] = {"recorded": applied.recorded}
        if refused:
            out["refused"] = refused
        out["still_needed"] = [field.key for field in progress.missing]
        if progress.unconfirmed:
            out["waiting_for_yes"] = [field.key for field in progress.unconfirmed]
        out["done"] = engine.done(record)
        return out


@dataclass(frozen=True)
class Applied:
    """What recording answers did to a record."""

    #: The fields it recorded (or confirmed, skipped or cleared).
    recorded: list[str]
    #: The fields it couldn't, and why.
    refused: dict[str, str]
    #: A value is new, different or gone (confirming one, or skipping an empty one, isn't a change).
    changed: bool


def apply_answers(
    design: IntakeDesign,
    record: IntakeRecord,
    *,
    answers: Mapping[str, Any] | None = None,
    inferred: Mapping[str, Any] | None = None,
    confirmed: Sequence[str] = (),
    skipped: Sequence[str] = (),
    cleared: Sequence[str] = (),
    others: bool = False,
) -> Applied:
    """
    Records what the person said into a record, each value checked as its
    field takes it; a change unconfirms the summary they said yes to.

    :param answers: What the person said, by field key.
    :param inferred: What was worked out from it (fields that may be inferred only).
    :param confirmed: Fields the person said are right.
    :param skipped: Optional fields they can't or won't answer.
    :param cleared: Fields to empty.
    :param others: Keys may be other intakes' (requests answered together):
        one this intake doesn't have is left alone, not refused.
    """
    before = record.values
    recorded: list[str] = []
    refused: dict[str, str] = {}

    def field_of(key: Any) -> IntakeField | None:
        field = design.field(str(key))
        if field is None and not others:
            refused[str(key)] = "it isn't one of the intake's fields"
        return field

    for key, value in (answers or {}).items():
        field = field_of(key)
        if field is None:
            continue
        try:
            record.answers[field.key] = Answer(value=checked(field, value), how="stated")
            recorded.append(field.key)
        except ValueRefused as exc:
            refused[field.key] = str(exc)
    for key, value in (inferred or {}).items():
        field = field_of(key)
        if field is None:
            continue
        current = record.answers.get(field.key)
        if not field.infer:
            refused[field.key] = "it can't be inferred: ask the person"
        elif current is not None and current.how in ("stated", "confirmed"):
            refused[field.key] = "the person said it: record a change under answers"
        else:
            try:
                record.answers[field.key] = Answer(value=checked(field, value), how="inferred")
                recorded.append(field.key)
            except ValueRefused as exc:
                refused[field.key] = str(exc)
    for key in confirmed:
        field = field_of(key)
        if field is None:
            continue
        current = record.answers.get(field.key)
        if current is None or current.how == "skipped":
            refused[field.key] = "it has no value to confirm"
        else:
            current.how = "confirmed"
            recorded.append(field.key)
    for key in skipped:
        field = field_of(key)
        if field is None:
            continue
        if field.required:
            refused[field.key] = "it's required, so it can't be skipped"
        else:
            record.answers[field.key] = Answer(value=None, how="skipped")
            recorded.append(field.key)
    for key in cleared:
        field = field_of(key)
        if field is not None and record.answers.pop(field.key, None) is not None:
            recorded.append(field.key)

    changed = before != record.values
    if changed:
        # What they said yes to isn't what's recorded now.
        record.summary_confirmed = False
    return Applied(recorded, refused, changed)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _keys(value: Any) -> Sequence[str]:
    if isinstance(value, str):
        return [value]
    return [str(key) for key in value] if isinstance(value, list) else []


def preview(
    name: str,
    config: Mapping[str, Any],
    answers: Mapping[str, Any],
    *,
    summary_confirmed: bool = False,
) -> dict[str, Any]:
    """
    What an intake would make of sample answers, for the builder's preview:
    where it would stand, what its model would be told before its next call,
    what an agent hands off to it with and what it would hand back.

    :param name: The node's name.
    :param config: The node's settings.
    :param answers: Sample answers by field key: a value, or ``{"value", "how"}``
        (``stated``, ``inferred``, ``handoff``, ``confirmed`` or ``skipped``).
    :param summary_confirmed: Whether the person said yes to the summary.
    """
    problems = design_problems(name, config)
    try:
        design = IntakeDesign.of(name, config)
    except ValidationError:
        return {"problems": problems}
    if len(design.fields) > MOST_FIELDS or len(design.sections) > MOST_SECTIONS:
        return {"problems": problems}
    record = IntakeRecord()
    refused: dict[str, str] = {}
    hows = set(How.__args__)  # type: ignore[attr-defined]
    for key, given in answers.items():
        field = design.field(str(key))
        if field is None:
            continue
        value, said = (
            (given.get("value"), given.get("how")) if isinstance(given, Mapping) else (given, "stated")
        )
        how: How = cast("How", said) if said in hows else "stated"
        if how == "skipped":
            if field.required:
                refused[field.key] = "it's required, so it can't be skipped"
            else:
                record.answers[field.key] = Answer(value=None, how="skipped")
            continue
        try:
            record.answers[field.key] = Answer(value=checked(field, value), how=how)
        except ValueRefused as exc:
            refused[field.key] = str(exc)
    engine = IntakeEngine(design, "intake")
    progress = progress_of(design, record)
    record.summary_confirmed = summary_confirmed and progress.ready
    try:
        handed = engine.input_schema().model_json_schema(by_alias=True)
    except (TypeError, ValueError):
        handed = None
    return {
        "problems": problems,
        "refused": refused,
        "applying": [field.key for field in progress.applying],
        "missing": [field.key for field in progress.missing],
        "unconfirmed": [field.key for field in progress.unconfirmed],
        "optional": [field.key for field in progress.optional],
        "locked": [section.id for section in progress.locked],
        "ask_next": [field.key for field in progress.ask_next],
        "next_section": progress.next_section.id if progress.next_section else None,
        "ready": progress.ready,
        "done": engine.done(record),
        "brief": engine.brief(record),
        "result": engine.result(record, "completed"),
        "input_schema": handed,
    }
