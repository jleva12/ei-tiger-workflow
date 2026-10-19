"""The caller's own layouts of the web console's dashboards.

A layout is the document the web console saves when a person arranges a
dashboard: which widgets it shows, where on its twelve-column grid and how
big, and each widget's settings. It's checked for its shape and size here;
which widget types there are, and their settings, are the web console's, which
reads a saved layout forgivingly. One layout per person and dashboard, in
every organization; API keys have none.
"""

from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import delete

from forge_admin.api.routes.common import Session, commit_or_conflict
from forge_admin.auth.access import CurrentPerson
from forge_admin.db.audit import UtcDateTime
from forge_admin.models import DashboardLayout

router = APIRouter(prefix="/me/dashboards", tags=["me"])

#: The dashboards people lay out.
Dashboard = Literal["organization-overview"]

#: The grid's columns: a widget's x and w count them.
GRID_COLUMNS = 12
MAX_WIDGETS = 64
MAX_SETTINGS = 32

WidgetId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
WidgetType = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
SettingName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
# Settings are plain values: a choice, a number, a switch.
SettingValue = (
    bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
    | Annotated[str, StringConstraints(max_length=500)]
    | None
)


class LayoutWidget(BaseModel):
    """One widget on a dashboard: an instance of a widget type, placed on the grid."""

    model_config = ConfigDict(extra="forbid")

    #: Unique in the layout.
    id: WidgetId
    #: One of the dashboard's widget types, as the web console names them.
    type: WidgetType
    #: Its first column, from 0.
    x: int = Field(ge=0, lt=GRID_COLUMNS)
    #: Where it starts, in pixels down; widgets rise as far as they can.
    y: int = Field(ge=0, le=1_000_000)
    #: Its width in columns.
    w: int = Field(ge=1, le=GRID_COLUMNS)
    #: Its least height in pixels; it grows to fit what it shows.
    h: int = Field(ge=1, le=4_000)
    #: Its own title, in place of its type's.
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    settings: dict[SettingName, SettingValue] | None = Field(
        default=None, max_length=MAX_SETTINGS
    )

    @model_validator(mode="after")
    def _on_the_grid(self) -> Self:
        if self.x + self.w > GRID_COLUMNS:
            raise ValueError(f"x + w must be at most {GRID_COLUMNS}")
        return self


class LayoutDocument(BaseModel):
    """A dashboard's layout, as the web console saves it."""

    model_config = ConfigDict(extra="forbid")

    #: The document's shape; the web console reads the versions it knows.
    version: int = Field(ge=1, le=1_000)
    widgets: list[LayoutWidget] = Field(max_length=MAX_WIDGETS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [widget.id for widget in self.widgets]
        if len(set(ids)) != len(ids):
            raise ValueError("widget IDs must be unique")
        return self


class SavedLayout(BaseModel):
    dashboard: Dashboard
    #: The layout as it was saved; null until the person saves one.
    layout: dict[str, Any] | None
    updated_at: UtcDateTime | None


@router.get("/{dashboard}")
async def get_dashboard_layout(
    dashboard: Dashboard, user: CurrentPerson, session: Session
) -> SavedLayout:
    """
    Return the caller's layout of a dashboard, or none when they haven't
    saved one and it shows its default.
    \f
    :param dashboard: The dashboard.
    :param user: The caller.
    :param session: The request's database session.
    :return: The saved layout; its ``layout`` is null when there's none.
    :raises HTTPException: 403 for an API key.
    """
    record = await session.get(DashboardLayout, (user, dashboard))
    return SavedLayout(
        dashboard=dashboard,
        layout=record.layout if record else None,
        updated_at=record.updated_at if record else None,
    )


@router.put("/{dashboard}")
async def save_dashboard_layout(
    dashboard: Dashboard, body: LayoutDocument, user: CurrentPerson, session: Session
) -> SavedLayout:
    """
    Save the caller's layout of a dashboard, in place of the last.
    \f
    :param dashboard: The dashboard.
    :param body: The layout.
    :param user: The caller.
    :param session: The request's database session.
    :return: The layout as saved.
    :raises HTTPException: 403 for an API key; 409 when their first layout of
        it is saved twice at once; 422 for a layout off the grid, with
        repeated widget IDs or over the limits.
    """
    layout = body.model_dump(mode="json", exclude_unset=True)
    record = await session.get(DashboardLayout, (user, dashboard))
    if record is None:
        record = DashboardLayout(user_id=user, dashboard=dashboard, layout=layout)
        session.add(record)
    else:
        record.layout = layout
    await commit_or_conflict(
        session, "The layout was saved elsewhere at once; try again"
    )
    return SavedLayout(
        dashboard=dashboard, layout=record.layout, updated_at=record.updated_at
    )


@router.delete("/{dashboard}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_dashboard_layout(
    dashboard: Dashboard, user: CurrentPerson, session: Session
) -> None:
    """
    Forget the caller's layout of a dashboard, so it shows its default
    again. Nothing to forget is no error.
    \f
    :param dashboard: The dashboard.
    :param user: The caller.
    :param session: The request's database session.
    :raises HTTPException: 403 for an API key.
    """
    await session.execute(
        delete(DashboardLayout).where(
            DashboardLayout.user_id == user, DashboardLayout.dashboard == dashboard
        )
    )
    await session.commit()
