"""Roles, permissions and the Casbin policy table they are enforced from."""

from sqlalchemy import BigInteger, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, ascii_string


class CasbinRule(AuditBase):
    """
    One Casbin policy line, in the layout Casbin's SQLAlchemy adapter reads.

    ``p`` lines grant a permission to a role (``v0`` role key, ``v1`` resource,
    ``v2`` action). ``g`` lines assign a role to a subject within a scope
    (``v0`` subject, ``v1`` role key, ``v2`` the scope's domain pattern, e.g.
    ``org:<id>*``). Unused columns stay NULL: the adapter stops
    reading a line at its first NULL.
    """

    __tablename__ = "casbin_rule"
    __table_args__ = (
        UniqueConstraint("ptype", "v0", "v1", "v2", name="uq_casbin_rule_line"),
        Index("ix_casbin_rule_ptype_v1_v2", "ptype", "v1", "v2"),
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

        :return: E.g. ``p, org:admin, organizations, update``.
        """
        values = [self.ptype]
        for value in (self.v0, self.v1, self.v2, self.v3, self.v4, self.v5):
            if value is None:
                break
            values.append(value)
        return ", ".join(values)


class Role(AuditBase):
    """
    A named set of permissions, assignable at one level of the hierarchy.

    The key (``<level>:<name>``, e.g. ``org:admin``) is the role's identity
    in Casbin policy lines and never changes; its level decides where it can
    be assigned. The name and description are for people.
    """

    __tablename__ = "authz_roles"

    key: Mapped[str] = mapped_column(ascii_string(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    @property
    def level(self) -> str:
        """
        :return: Where the role can be assigned: site or org.
        """
        return self.key.split(":", 1)[0]


class Permission(AuditBase):
    """
    An action on a resource that roles can be granted, written as one key,
    ``resource:action`` (e.g. ``workflows:run``). Either part may be ``*``.
    """

    __tablename__ = "authz_permissions"
    __table_args__ = (UniqueConstraint("resource", "action"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource: Mapped[str] = mapped_column(ascii_string(255))
    action: Mapped[str] = mapped_column(ascii_string(100))
    description: Mapped[str] = mapped_column(Text, default="")

    @property
    def key(self) -> str:
        """
        The permission as one string.

        :return: ``resource:action``, e.g. ``workflows:run``.
        """
        return f"{self.resource}:{self.action}"
