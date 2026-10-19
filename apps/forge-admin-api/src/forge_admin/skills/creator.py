"""The skill creator's kit and its hand-in toolset: the Python behind the
``skill_creator`` helper (``forge_admin/helpers/helpers.yaml`` holds its
instruction, toolsets and limits).

It's sent what a person describes, and the skill to rework when there is
one. It looks at what the organization has with the ``organization``
toolset (``forge_admin.helpers.organization``) and hands the skill in with
``save_skill`` (the ``skill_vault`` toolset), which holds it to the skill
format (``forge_agent_runtime.skills``) and tells it what to fix; the last
skill it saved is the one kept. It runs in the background: when it's done,
:func:`finish` adds the skill to the vault (or reworks the one it was
given) and the job says which.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from typing import Annotated, Any

from forge_agent_runtime import SkillBundle, SkillFile
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from forge_admin.helpers.kit import (
    Finished,
    HelperError,
    HelperRun,
    HelperToolset,
    Kit,
    Outcome,
    Refused,
)
from forge_admin.helpers.organization import clip
from forge_admin.models import Skill
from forge_admin.skills.vault import fill

#: The most notes a job keeps.
MOST_NOTES = 5
NOTE_CHARS = 400


class CreatorInput(BaseModel):
    """What the skill creator is asked."""

    model_config = ConfigDict(extra="forbid")

    #: What the skill should do: the person's description.
    brief: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ]
    #: One of the organization's skills to rework; None for a new one.
    revises: str | None = Field(default=None, max_length=36)


def vault_tools(run: HelperRun) -> list[BaseTool]:
    """``save_skill``: hands the skill in to ``run``."""

    async def save_skill(
        name: str,
        description: str,
        instructions: str,
        files: list[dict[str, str]] | None = None,
        notes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Hands the skill in. Call it again with the whole skill to change it.

        Args:
          name: Lowercase words joined by hyphens, e.g. triage-support-tickets.
          description: What it does and when to use it (at most 1024 characters).
          instructions: The SKILL.md body: Markdown, without frontmatter.
          files: Its other files, each {"path": "references/fields.md", "content": "..."}.
          notes: For the person: what to check, connect or decide; at most five.
        """
        problems: list[str] = []
        made: list[SkillFile] = []
        for item in files or []:
            if not isinstance(item, Mapping):
                problems.append("each file is {path, content}")
                continue
            try:
                made.append(
                    SkillFile(
                        path=str(item.get("path") or ""),
                        content=str(item.get("content") or ""),
                    )
                )
            except ValueError as error:
                problems.append(_problem(error))
        body = re.sub(r"^---\n.*?\n---\n", "", instructions.strip(), flags=re.DOTALL)
        if problems:
            return {"saved": False, "problems": problems}
        try:
            skill = SkillBundle(
                name=name.strip(),
                description=description,
                instructions=body.strip(),
                files=made,
            )
        except ValueError as error:
            return {"saved": False, "problems": [_problem(error)]}
        if not skill.instructions:
            return {"saved": False, "problems": ["write its instructions"]}
        kept = [clip(note, NOTE_CHARS) for note in notes or [] if str(note).strip()]
        run.hand_in(skill, kept[:MOST_NOTES])
        return {"saved": True, "name": skill.name, "files": len(skill.files)}

    return [FunctionTool(save_skill)]


VAULT_TOOLSET = HelperToolset(
    name="skill_vault",
    description="save_skill: hands a skill in, held to the skill format.",
    tools=frozenset({"save_skill"}),
    build=vault_tools,
)


def _problem(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        return "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc') or ())}: "
            f"{str(e.get('msg') or '').removeprefix('Value error, ')}".lstrip(": ")
            for e in errors()
        )
    return str(error)


def prompt(brief: str, revising: SkillBundle | None) -> str:
    """What the creator is sent: the person's description, and the skill to rework."""
    parts = ["# What the person wants", brief.strip()]
    if revising is not None:
        parts += [
            "# The skill to rework",
            "Keep its name unless the person asks otherwise. Its current SKILL.md and files:",
            revising.skill_md(),
            json.dumps(
                [
                    {"path": f.path, "content": f.content[:4_000]}
                    for f in revising.files
                    if f.encoding == "utf-8"
                ][:20],
                ensure_ascii=False,
            ),
        ]
    return "\n\n".join(parts)


async def prepare(run: HelperRun, given: CreatorInput, session: AsyncSession) -> str:
    """
    :raises Refused: 404 when the skill to rework isn't the organization's.
    """
    revising = None
    if given.revises:
        row = await session.scalar(
            select(Skill)
            .options(undefer(Skill.bundle))
            .where(
                Skill.id == given.revises,
                Skill.organization_id == run.organization_id,
            )
        )
        if row is None:
            raise Refused(404, "The organization has no such skill")
        revising = SkillBundle.model_validate(row.bundle)
    return prompt(given.brief, revising)


async def finish(run: HelperRun, given: CreatorInput, outcome: Outcome) -> Finished:
    """Adds the skill it saved to the vault (or reworks the one it was given)."""
    if outcome.handed_in is None:
        raise HelperError("The creator finished without saving a skill; try again.")
    skill: SkillBundle = outcome.handed_in.value
    async with run.sessions() as session:
        row = (
            await session.scalar(
                select(Skill)
                .options(undefer(Skill.bundle))
                .where(
                    Skill.id == given.revises,
                    Skill.organization_id == run.organization_id,
                )
            )
            if given.revises
            else None
        )
        name = await free_name(
            session, run.organization_id, skill.name, keep=row.id if row else None
        )
        if row is None:
            row = Skill(organization_id=run.organization_id)
            session.add(row)
        fill(row, skill.model_copy(update={"name": name}))
        row.source = {"kind": "creator", "job": run.job_id}
        await session.flush()
        finished = Finished(
            {"skill_id": row.id, "name": row.name}, outcome.handed_in.notes
        )
        await session.commit()
    return finished


KIT = Kit(name="skill_creator", input=CreatorInput, prepare=prepare, finish=finish)


async def free_name(
    session: AsyncSession, organization_id: str, name: str, *, keep: str | None = None
) -> str:
    """:return: ``name``, or ``name-2``, ``name-3``…: one no other skill of the
    organization has."""
    taken = set(
        await session.scalars(
            select(Skill.name).where(
                Skill.organization_id == organization_id,
                Skill.name.like(f"{name[:58]}%"),
                *([Skill.id != keep] if keep else []),
            )
        )
    )
    if name not in taken:
        return name
    for n in range(2, 1_000):
        candidate = f"{name[: 64 - len(str(n)) - 1]}-{n}"
        if candidate not in taken:
            return candidate
    return f"{name[:55]}-{uuid.uuid4().hex[:8]}"
