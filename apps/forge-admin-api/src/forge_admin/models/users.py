"""The people who use Forge: who roles are assigned to."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, ascii_string
from forge_admin.models.hierarchy import new_id


class User(AuditBase):
    """
    A person who uses Forge, to pick when assigning roles.

    The ID is the subject their Casbin role assignments name
    (``g, <id>, org:admin, ...``) and the ``sub`` of their bearer tokens, so it
    follows ``SUBJECT_PATTERN``. A new user gets a random UUID unless one is
    given, e.g. an identity provider's subject or an existing assignment's.

    :ivar id: The user's subject ID.
    :type id: str
    :ivar first_name: Their first name.
    :type first_name: str
    :ivar last_name: Their last name.
    :type last_name: str
    :ivar email: Their email address, stored in lowercase; unique.
    :type email: str
    :ivar msid: Their MS ID (network user ID), stored in lowercase; unique.
    :type msid: str
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(ascii_string(255), primary_key=True, default=new_id)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    msid: Mapped[str] = mapped_column(ascii_string(64), unique=True)
