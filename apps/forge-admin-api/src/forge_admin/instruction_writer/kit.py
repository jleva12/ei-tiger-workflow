"""The instruction writer's kit: the Python behind the ``instruction_writer``
helper (``forge_admin/helpers/helpers.yaml`` holds its instruction and
limits).

It's sent what the person wants the agent to do, the instruction it has now
(to rework, or none), and the brief of the agent as it's set up
(:mod:`.brief`, filled in from the organization's records). It answers with
the instruction between ``<instruction>`` tags and a few notes for the
person between ``<notes>`` tags (:func:`parse`).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.helpers.kit import Finished, HelperError, HelperRun, Kit, Outcome
from forge_admin.instruction_writer.brief import Brief, render, resolve

#: The most notes a draft has.
MOST_NOTES = 4
NOTE_CHARS = 300


class WriteInput(BaseModel):
    """What the builders send the writer."""

    model_config = ConfigDict(extra="forbid")

    #: What the agent should do, or what to change in ``current``.
    ask: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ]
    #: The instruction to rework: the field's, or the last draft. Empty for a new one.
    current: str = Field(default="", max_length=60_000)
    brief: Brief


def prompt(ask: str, current: str, brief: str) -> str:
    """What the writer is sent: the brief, the instruction now, and the ask."""
    now = current.strip()
    return "\n\n".join(
        [
            brief,
            "# Its instruction now",
            f"<current>\n{now}\n</current>" if now else "None yet: write a new one.",
            "# What the person wants",
            ask.strip(),
        ]
    )


async def prepare(run: HelperRun, given: WriteInput, session: AsyncSession) -> str:
    found = await resolve(
        given.brief,
        run.organization_id,
        session,
        chat_agents=getattr(run.state, "chat_agents", None),
        workflows=getattr(run.state, "organization_agents", None),
        ui_components=getattr(run.state, "ui_components", None),
    )
    return prompt(given.ask, given.current, render(given.brief, found))


async def finish(run: HelperRun, given: WriteInput, outcome: Outcome) -> Finished:
    instruction, notes = parse(outcome.text)
    return Finished({"instruction": instruction}, notes)


KIT = Kit(name="instruction_writer", input=WriteInput, prepare=prepare, finish=finish)


_INSTRUCTION = re.compile(r"<instruction>\s*(.*?)\s*(?:</instruction>|$)", re.DOTALL)
_NOTES = re.compile(r"<notes>(.*?)(?:</notes>|$)", re.DOTALL)


def parse(text: str) -> tuple[str, list[str]]:
    """
    :param text: The writer's answer.
    :return: The instruction and the notes in it. An answer without the tags
        is all instruction.
    :raises HelperError: There's no instruction in it.
    """
    found = _INSTRUCTION.search(text)
    instruction = (found.group(1) if found else text.split("<notes>")[0]).strip()
    # A model may fence the whole instruction.
    fenced = re.fullmatch(r"```(?:markdown|md)?\n(.*)\n```", instruction, re.DOTALL)
    if fenced:
        instruction = fenced.group(1).strip()
    if not instruction:
        raise HelperError("The writer answered without an instruction; try again.")
    notes: list[str] = []
    said = _NOTES.search(text)
    for line in said.group(1).splitlines() if said else ():
        note = line.strip().lstrip("-*• ").strip()
        if note and note.lower() not in {"none", "none."}:
            notes.append(note[:NOTE_CHARS])
    return instruction, notes[:MOST_NOTES]
