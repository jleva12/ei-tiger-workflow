"""The instruction editor's kit: the Python behind the ``instruction_editor``
helper (``forge_admin/helpers/helpers.yaml`` holds its instruction and
limits), the AI inside the rich text editor of an agent's instruction.

Where the instruction writer writes the whole instruction, the editor works
on part of it: it rewrites the passage the person selected, or writes what
goes in at the caret, as the person (or the menu item they chose) asks. It's
sent the instruction as it is, the selected passage, the text around the
caret, where its answer goes, and, when the builder has one, the brief of
the agent (:mod:`.brief`), so it names the agent's real tools and values. It
answers with the Markdown between ``<text>`` tags (:func:`parse`).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.helpers.kit import Finished, HelperError, HelperRun, Kit, Outcome
from forge_admin.instruction_writer.brief import Brief, render, resolve

#: The most of the text around the caret sent, each side.
AROUND_CHARS = 8_000

Place = Literal["replace", "insert", "append", "preview"]

#: Where the answer goes, as the editor is told.
PLACES: dict[Place, str] = {
    "replace": "It replaces the selected passage, so it must stand in its place.",
    "insert": (
        "It goes in at the caret, after the selected passage if there is one, "
        "between the text before and after: it must read on from the one and "
        "into the other."
    ),
    "append": "It goes at the end of the instruction.",
    "preview": "The person reads it first and chooses where it goes.",
}


class EditInput(BaseModel):
    """What the rich text editor sends for one AI action."""

    model_config = ConfigDict(extra="forbid")

    #: The editor's action (``rewrite``, ``insert``, a custom one): for the record.
    action: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    #: What to do: the action's instruction, or what the person typed.
    ask: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ]
    #: The whole instruction now, in Markdown.
    document: str = Field(default="", max_length=60_000)
    #: The selected passage, in Markdown; empty at a caret.
    selection: str = Field(default="", max_length=60_000)
    #: The Markdown just before and after the selection or caret.
    before: str = Field(default="", max_length=AROUND_CHARS)
    after: str = Field(default="", max_length=AROUND_CHARS)
    #: Where the answer goes.
    place: Place = "replace"
    #: The agent as it's set up, when the builder has it.
    brief: Brief | None = None


def prompt(given: EditInput, brief: str | None) -> str:
    """What the editor is sent: the setup, the instruction, the passage, and the ask."""
    parts: list[str] = [brief] if brief else []
    document = given.document.strip()
    parts += [
        "# The instruction now",
        f"<document>\n{document}\n</document>" if document else "Empty so far.",
    ]
    selection = given.selection.strip()
    if selection:
        parts += ["# The selected passage", f"<selection>\n{selection}\n</selection>"]
    parts += ["# Where your text goes", PLACES[given.place]]
    if given.place == "insert" and (given.before.strip() or given.after.strip()):
        parts += [
            f"<before>\n{given.before.rstrip()}\n</before>",
            f"<after>\n{given.after.lstrip()}\n</after>",
        ]
    parts += ["# What to do", given.ask]
    return "\n\n".join(parts)


async def prepare(run: HelperRun, given: EditInput, session: AsyncSession) -> str:
    rendered = None
    if given.brief is not None:
        found = await resolve(
            given.brief,
            run.organization_id,
            session,
            chat_agents=getattr(run.state, "chat_agents", None),
            workflows=getattr(run.state, "organization_agents", None),
        )
        rendered = render(given.brief, found)
    return prompt(given, rendered)


async def finish(run: HelperRun, given: EditInput, outcome: Outcome) -> Finished:
    return Finished({"markdown": parse(outcome.text)})


KIT = Kit(name="instruction_editor", input=EditInput, prepare=prepare, finish=finish)


_TEXT = re.compile(r"<text>\s*(.*?)\s*(?:</text>|$)", re.DOTALL)


def parse(text: str) -> str:
    """
    :param text: The editor's answer.
    :return: The Markdown in it. An answer without the tags is all Markdown.
    :raises HelperError: It's empty.
    """
    found = _TEXT.search(text)
    markdown = (found.group(1) if found else text).strip()
    # A model may fence the whole answer.
    fenced = re.fullmatch(r"```(?:markdown|md)?\n(.*)\n```", markdown, re.DOTALL)
    if fenced:
        markdown = fenced.group(1).strip()
    if not markdown:
        raise HelperError("The AI answered with nothing; try again.")
    return markdown
