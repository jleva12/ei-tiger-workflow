"""Widgets people share with their organization, for its members to add.

A person who defined a widget on a dashboard (a ``custom`` one) may share it
with an organization they see; its members then add it from the dashboard's
widget menu, each taking a copy into their own layout. Who shared it may take
it away again, and so may whoever holds ``shared_widgets:manage`` there (its
administrators); the copies people added stay theirs.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select

from forge_admin.api.routes.common import NodeId, Session, name_of
from forge_admin.api.routes.dashboards import (
    MAX_SETTINGS,
    Dashboard,
    SettingName,
    SettingValue,
)
from forge_admin.auth.access import (
    CurrentPerson,
    Enforcer,
    Level,
    Scope,
    allows,
    authorize,
)
from forge_admin.db.audit import UtcDateTime
from forge_admin.models import SharedWidget

router = APIRouter(
    prefix="/organizations/{organization_id}/dashboards/{dashboard}/shared-widgets",
    tags=["dashboards"],
)

READ = "organizations:read"
MANAGE = "shared_widgets:manage"
#: The most widgets an organization shares for a dashboard.
MAX_SHARED = 200


class SharedWidgetIn(BaseModel):
    """A widget to share, as a layout keeps it, less its place."""

    model_config = ConfigDict(extra="forbid")

    #: Only widgets people define are shared; the dashboard's own are everyone's.
    type: Literal["custom"]
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    settings: dict[SettingName, SettingValue] = Field(
        default_factory=dict, max_length=MAX_SETTINGS
    )


class SharedWidgetRead(BaseModel):
    id: str
    type: str
    title: str
    settings: dict[str, Any]
    #: Who shared it: their subject ID, and their name as members know it.
    shared_by: str
    shared_by_name: str
    shared_at: UtcDateTime
    #: Whether the caller may take it away: they shared it, or manage them.
    can_remove: bool


def _read(record: SharedWidget, name: str, can_remove: bool) -> SharedWidgetRead:
    return SharedWidgetRead(
        id=record.id,
        type=record.type,
        title=record.title,
        settings=record.settings,
        shared_by=record.created_by,
        shared_by_name=name,
        shared_at=record.created_at,
        can_remove=can_remove,
    )


@router.get("")
async def list_shared_widgets(
    organization_id: NodeId,
    dashboard: Dashboard,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> list[SharedWidgetRead]:
    """
    List the widgets shared with an organization for a dashboard, newest
    first.
    \f
    :raises HTTPException: 403 for an API key, or without organizations:read
        in the organization; 404 for no such organization.
    """
    domain = await authorize(
        session, enforcer, user, READ, Scope(Level.ORG, organization_id)
    )
    manages = allows(enforcer, user, MANAGE, domain)
    records = (
        await session.scalars(
            select(SharedWidget)
            .where(
                SharedWidget.organization_id == organization_id,
                SharedWidget.dashboard == dashboard,
            )
            .order_by(SharedWidget.created_at.desc())
        )
    ).all()
    names = {
        subject: await name_of(session, subject)
        for subject in {record.created_by for record in records}
    }
    return [
        _read(record, names[record.created_by], manages or record.created_by == user)
        for record in records
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def share_widget(
    organization_id: NodeId,
    dashboard: Dashboard,
    body: SharedWidgetIn,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> SharedWidgetRead:
    """
    Share a widget with an organization, for its members to add to their
    dashboards.
    \f
    :raises HTTPException: 403 for an API key, or without organizations:read
        in the organization; 404 for no such organization; 409 when it
        already shares as many as it may; 422 for a widget that isn't one.
    """
    await authorize(session, enforcer, user, READ, Scope(Level.ORG, organization_id))
    shared = await session.scalar(
        select(func.count())
        .select_from(SharedWidget)
        .where(
            SharedWidget.organization_id == organization_id,
            SharedWidget.dashboard == dashboard,
        )
    )
    if (shared or 0) >= MAX_SHARED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"The organization already shares {MAX_SHARED} widgets here; "
            "take some away first",
        )
    record = SharedWidget(
        organization_id=organization_id,
        dashboard=dashboard,
        type=body.type,
        title=body.title,
        settings=body.model_dump(mode="json")["settings"],
    )
    session.add(record)
    await session.commit()
    return _read(record, await name_of(session, user), True)


@router.delete("/{widget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_shared_widget(
    organization_id: NodeId,
    dashboard: Dashboard,
    widget_id: NodeId,
    user: CurrentPerson,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Take a shared widget away from an organization. The copies members added
    stay on their dashboards.
    \f
    :raises HTTPException: 403 for an API key, without organizations:read in
        the organization, or for someone else's widget without
        shared_widgets:manage there; 404 for no such widget there.
    """
    domain = await authorize(
        session, enforcer, user, READ, Scope(Level.ORG, organization_id)
    )
    record = await session.get(SharedWidget, widget_id)
    if (
        record is None
        or record.organization_id != organization_id
        or record.dashboard != dashboard
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such shared widget")
    if record.created_by != user and not allows(enforcer, user, MANAGE, domain):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Requires {MANAGE}, or to have shared it",
        )
    await session.delete(record)
    await session.commit()
