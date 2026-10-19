"""An organization's own agent permissions: who may use its agents.

Apart from the site's roles and permissions (forge_admin.models.authorization),
which decide what people see and do in Forge itself, each organization sets
up its own agent permissions and links them to the company's groups (and its
own API keys). There are no agent roles: the directory's groups say who's
who. They count only in that organization.
"""

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, ascii_string


class AgentCasbinRule(AuditBase):
    """
    One line of the organizations' agent access policy, in the layout
    Casbin's SQLAlchemy adapter reads (``agent_casbin_model.conf``). Every
    line is a ``p`` line naming its organization's domain, ``org:<id>``:

    - ``p, group:<name>, org:<id>, <resource>, <action>``: a company group is
      linked to an agent permission.
    - ``p, apikey:<id>, org:<id>, <resource>, <action>``: one of the
      organization's API keys is.
    """

    __tablename__ = "agent_casbin_rule"
    __table_args__ = (
        UniqueConstraint(
            "ptype", "v0", "v1", "v2", "v3", name="uq_agent_casbin_rule_line"
        ),
        Index("ix_agent_casbin_rule_ptype_v1_v2", "ptype", "v1", "v2"),
        Index("ix_agent_casbin_rule_ptype_v2", "ptype", "v2"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ptype: Mapped[str] = mapped_column(ascii_string(8))
    v0: Mapped[str | None] = mapped_column(ascii_string(255))
    v1: Mapped[str | None] = mapped_column(ascii_string(255))
    v2: Mapped[str | None] = mapped_column(ascii_string(255))
    v3: Mapped[str | None] = mapped_column(ascii_string(255))
    v4: Mapped[str | None] = mapped_column(ascii_string(255))
    v5: Mapped[str | None] = mapped_column(ascii_string(255))

    def __str__(self) -> str:
        """
        Render the row as a Casbin policy line; the adapter loads rows this way.

        :return: E.g. ``p, group:Finance, org:<id>, invoices, approve``.
        """
        values = [self.ptype]
        for value in (self.v0, self.v1, self.v2, self.v3, self.v4, self.v5):
            if value is None:
                break
            values.append(value)
        return ", ".join(values)


class AgentPermission(AuditBase):
    """
    One of an organization's agent permissions, ``resource:action`` (e.g.
    ``invoices:approve``); either part may be ``*``. The organization names
    its own; they mean what its agents will check them for.
    """

    __tablename__ = "agent_permissions"
    __table_args__ = (UniqueConstraint("organization_id", "resource", "action"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    resource: Mapped[str] = mapped_column(ascii_string(255))
    action: Mapped[str] = mapped_column(ascii_string(100))
    description: Mapped[str] = mapped_column(Text, default="")

    @property
    def key(self) -> str:
        """
        :return: ``resource:action``, e.g. ``invoices:approve``.
        """
        return f"{self.resource}:{self.action}"


class AgentGroupLink(AuditBase):
    """
    One of the company's groups, from its external directory, linked to an
    organization's agent permissions: someone whose sign-in names the group
    holds them in that organization. The grants are ``p`` lines for the
    subject ``group:<name>`` in the organization's domain.
    """

    __tablename__ = "agent_group_links"
    __table_args__ = (UniqueConstraint("organization_id", "group_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    #: The group's name, as the directory and tokens spell it.
    group_name: Mapped[str] = mapped_column(ascii_string(200))
    description: Mapped[str] = mapped_column(Text, default="")
