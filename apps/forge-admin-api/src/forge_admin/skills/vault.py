"""The organization's skills as what runs reads them: by ID, whole, and only
its own.

- :func:`fill`: a row made a skill, however it got into the vault.
- :func:`bundles_of`: skills by ID, for a workflow run to carry and an
  exported agent to bundle.
- :class:`OrganizationSkills`: the hosted runtime's ``SkillLibrary``.
- :func:`skills_used`: the skills a chat agent's Skills nodes name.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from forge_agent_runtime import SkillBundle
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from forge_admin.models import Skill


def fill(row: Skill, skill: SkillBundle) -> None:
    """Make the row this skill."""
    row.name = skill.name
    row.description = skill.description
    row.bundle = skill.model_dump(mode="json")
    row.file_count = len(skill.files)
    row.size_bytes = skill.size


async def bundles_of(
    session: AsyncSession, organization_id: str, skill_ids: Iterable[str]
) -> dict[str, SkillBundle]:
    """:return: Those of the skills that are the organization's, whole, by ID."""
    ids = list(dict.fromkeys(skill_ids))
    if not ids:
        return {}
    rows = await session.scalars(
        select(Skill)
        .options(undefer(Skill.bundle))
        .where(Skill.organization_id == organization_id, Skill.id.in_(ids))
    )
    return {row.id: SkillBundle.model_validate(row.bundle) for row in rows}


class OrganizationSkills:
    """
    The skills an agent's Skills tools name, as the hosted runtime builds
    them (``forge_agent_runtime.skills.SkillLibrary``): its organization's,
    as they're saved now.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def __call__(
        self, skill_ids: Sequence[str], *, organization_id: str | None
    ) -> list[SkillBundle]:
        if organization_id is None:
            raise LookupError("The agent belongs to no organization.")
        async with self.sessions() as session:
            found = await bundles_of(session, organization_id, skill_ids)
        missing = [skill_id for skill_id in skill_ids if skill_id not in found]
        if missing:
            raise LookupError(
                f"the organization has no skill {missing[0]}: it was deleted; pick another"
            )
        return [found[skill_id] for skill_id in skill_ids]


class SkillStamps:
    """
    When the skills an agent uses last changed, for the hosted runtime's
    cache: an agent built before one of them changed is built again.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def __call__(
        self, organization_id: str | None, document: Mapping[str, Any]
    ) -> str:
        """:return: Empty for an agent without skills; otherwise how many of
        them there are and when the latest changed."""
        ids = skills_used(document)
        if not ids or organization_id is None:
            return ""
        async with self.sessions() as session:
            count, latest = (
                await session.execute(
                    select(func.count(Skill.id), func.max(Skill.updated_at)).where(
                        Skill.organization_id == organization_id, Skill.id.in_(ids)
                    )
                )
            ).one()
        return f"{count}:{latest.isoformat() if latest else ''}"


def skills_used(document: Mapping[str, Any]) -> list[str]:
    """:return: The skills a chat agent's Skills nodes name, by ID, in order."""
    ids: list[str] = []
    for node in document.get("nodes") or []:
        if not isinstance(node, Mapping) or node.get("kind") != "skills":
            continue
        config = node.get("config")
        for skill_id in (
            config.get("skills") if isinstance(config, Mapping) else None
        ) or []:
            if isinstance(skill_id, str) and skill_id and skill_id not in ids:
                ids.append(skill_id)
    return ids
