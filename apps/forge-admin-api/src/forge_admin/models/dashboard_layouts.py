"""How people lay out the web console's dashboards, and the widgets they share."""

from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AuditBase, JSONText, ascii_string
from forge_admin.models.hierarchy import new_id


class DashboardLayout(AuditBase):
    """
    A person's layout of one dashboard, such as the organization overview:
    which widgets it shows, where and how big, as the web console's layout
    document (``forge_admin.api.routes.dashboards.LayoutDocument``). One per
    person and dashboard, the same in every organization; without one, the
    dashboard shows its default.

    :ivar user_id: The person's subject ID.
    :type user_id: str
    :ivar dashboard: The dashboard, e.g. ``organization-overview``.
    :type dashboard: str
    :ivar layout: The layout document.
    :type layout: dict[str, Any]
    """

    __tablename__ = "dashboard_layouts"

    user_id: Mapped[str] = mapped_column(ascii_string(255), primary_key=True)
    dashboard: Mapped[str] = mapped_column(ascii_string(64), primary_key=True)
    layout: Mapped[dict[str, Any]] = mapped_column(JSONText)


class SharedWidget(AuditBase):
    """
    A widget a person defined and shared with their organization, for its
    members to add to their own dashboards: a copy each, so a shared widget
    taken away leaves theirs be. Who shared it is ``created_by``; they, and
    those with ``shared_widgets:manage`` there, may take it away.

    :ivar id: Its ID.
    :type id: str
    :ivar organization_id: The organization it's shared with.
    :type organization_id: str
    :ivar dashboard: The dashboard it's for, e.g. ``organization-overview``.
    :type dashboard: str
    :ivar type: Its widget type, e.g. ``custom``.
    :type type: str
    :ivar title: Its title.
    :type title: str
    :ivar settings: Its settings, as a layout keeps a widget's.
    :type settings: dict[str, Any]
    """

    __tablename__ = "shared_widgets"
    __table_args__ = (
        Index(
            "ix_shared_widgets_library", "organization_id", "dashboard", "created_at"
        ),
    )

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    dashboard: Mapped[str] = mapped_column(ascii_string(64))
    type: Mapped[str] = mapped_column(ascii_string(64))
    title: Mapped[str] = mapped_column(String(120))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONText, default=dict)
