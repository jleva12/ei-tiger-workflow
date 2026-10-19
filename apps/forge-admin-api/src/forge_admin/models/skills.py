"""Organizations' skills vault."""

from typing import Any
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, deferred, mapped_column

from forge_admin.db.base import AuditBase, JSONText, ascii_string


def new_id() -> str:
    """:return: A random UUID, a new skill's ID."""
    return str(uuid4())


class Skill(AuditBase):
    """
    One of an organization's skills (Agent Skills: a SKILL.md and the files
    beside it): what its agents' Skills tools, and its workflows' LLM agents',
    name by ID and load when a task calls for one.

    The whole skill is ``bundle``, a ``forge_agent_runtime.skills.SkillBundle``'s
    JSON, read only when it's needed (listings don't); its name and
    description are kept beside it to list it by. ``source`` says where it
    came from: written here, uploaded, imported from GitHub (the repository,
    path, ref and commit, so it can be pulled again) or written by the skill
    creator.
    """

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    #: The skill's name, as its SKILL.md has it (lowercase words joined by hyphens).
    name: Mapped[str] = mapped_column(ascii_string(64))
    description: Mapped[str] = mapped_column(Text, default="")
    bundle: Mapped[dict[str, Any]] = deferred(mapped_column(JSONText, nullable=False))
    #: How many files it has beside its SKILL.md, and its size in bytes.
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: ``{"kind": "written" | "upload" | "github" | "creator", ...}``.
    source: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
