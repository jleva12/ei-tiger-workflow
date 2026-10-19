"""What a helper's Python provides, and what the executor hands it.

A helper's configuration (``helpers.yaml``) says how it runs; its
:class:`Kit` says what it reads and writes: the input its request carries
(validated against :attr:`Kit.input`), the message the model is sent
(:attr:`Kit.prepare`), and what its answer becomes (:attr:`Kit.finish`).
Several helpers may share a kit, with different instructions or tools.

Its tools come from named :class:`HelperToolset`\\ s, built for each run
over its :class:`HelperRun`: they read only the organization the run is for.
A toolset's hand-in tool (``hand_in`` in the configuration) gives the
helper's result to :meth:`HelperRun.hand_in` instead of answering it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from google.adk.tools.base_tool import BaseTool
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class HelperError(Exception):
    """
    A helper couldn't do its work: the model failed, stopped, answered
    nothing usable or took too long. Its message is for the person.

    :param status_code: The HTTP status a request that waited for it gets:
        502 (the model's fault) unless said otherwise.
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class Refused(Exception):
    """A kit won't start on this input, e.g. it names another organization's record."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class HandedIn:
    """What a hand-in tool accepted: the result, and notes for the person."""

    value: Any
    notes: list[str] = field(default_factory=list)


@dataclass
class HelperRun:
    """
    One run of a helper, as its kit and its toolsets see it.

    :ivar helper: The helper's name.
    :ivar organization_id: The organization it's for: only its records are read.
    :ivar user_id: Who asked; its model calls count for them.
    :ivar state: The app's state: its stores (``chat_agents``,
        ``organization_agents``, ``knowledge_search``, …) and ``sessionmaker``.
    :ivar job_id: Its job, when it runs in the background.
    :ivar handed_in: What its hand-in tool accepted, in order.
    """

    helper: str
    organization_id: str
    user_id: str
    state: Any
    job_id: str | None = None
    handed_in: list[HandedIn] = field(default_factory=list)

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        """Database sessions, for work beside the request (tools, jobs)."""
        return self.state.sessionmaker  # type: ignore[no-any-return]

    def hand_in(self, value: Any, notes: Iterable[str] = ()) -> None:
        """From a hand-in tool: this is the result (the last one handed in is kept)."""
        self.handed_in.append(HandedIn(value, list(notes)))


@dataclass(frozen=True)
class Outcome:
    """
    What a run came to.

    :ivar text: Its last answer.
    :ivar handed_in: The last thing its hand-in tool accepted; None without one.
    :ivar model: The model it ran on (``provider/model``).
    """

    text: str
    handed_in: HandedIn | None
    model: str


@dataclass(frozen=True)
class Finished:
    """
    A helper's result, as its request or job gives it.

    :ivar result: JSON: what the helper made.
    :ivar notes: For the person: what to check, attach or decide.
    :ivar model: The model it ran on; the executor fills it in.
    """

    result: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    model: str = ""


@dataclass(frozen=True)
class Kit[I: BaseModel]:
    """
    A helper's Python.

    :ivar name: What ``kit`` names it by in the configuration.
    :ivar input: Its request's body.
    :ivar prepare: The message the model is sent, from the input and the
        organization's records (the request's database session); raises
        :class:`Refused` for input it won't start on.
    :ivar finish: The run's outcome as a result; raises :class:`HelperError`
        for an answer it can't use.
    """

    name: str
    input: type[I]
    prepare: Callable[[HelperRun, I, AsyncSession], Awaitable[str]]
    finish: Callable[[HelperRun, I, Outcome], Awaitable[Finished]]


@dataclass(frozen=True)
class HelperToolset:
    """
    Tools the configuration can give helpers, by name.

    :ivar name: Its name in the configuration.
    :ivar description: What its tools do, for whoever writes the configuration.
    :ivar tools: Its tools' names, which the configuration is checked against.
    :ivar build: Its tools for one run.
    """

    name: str
    description: str
    tools: frozenset[str]
    build: Callable[[HelperRun], Sequence[BaseTool]]
