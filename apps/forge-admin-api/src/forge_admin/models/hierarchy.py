"""The hierarchy: the site, and the organizations in it, where the work happens."""

from uuid import uuid4

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, ascii_string


def new_id() -> str:
    """
    Generate the ID of a new organization.

    :return: A random UUID, e.g. ``3f6c...-...``.
    """
    return str(uuid4())


class Organization(AuditBase):
    """
    Represents an organization entity in the database.

    This class defines the structure of an organization, including its unique
    identifier, name, and description. It is used to store and manage information
    related to organizations, such as companies, groups, or other entities.

    :ivar id: The unique identifier of the organization.
    :type id: str
    :ivar name: The name of the organization. Must be unique among all organizations.
    :type name: str
    :ivar description: A textual description of the organization. Defaults to an
        empty string if not provided.
    :type description: str
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
