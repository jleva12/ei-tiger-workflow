"""Every time the API sends carries its zone, so browsers show it in their own.

MySQL keeps times without a zone, written in UTC. Sent as they are, a browser
reads "2026-09-27T12:00:00" as its own local time and shows it hours off.
"""

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, get_args, get_origin

from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.routing import BaseRoute

from forge_admin.api.app import PUBLIC_ROUTERS, ROUTERS
from forge_admin.api.routes.organizations import OrganizationRead
from forge_admin.db.audit import UtcDateTime, as_utc


def _times_without_zone(annotation: Any, where: str, seen: set[type]) -> Iterator[str]:
    if get_origin(annotation) is Annotated:
        inner, *metadata = get_args(annotation)
        if inner is datetime:
            if not any(getattr(item, "func", None) is as_utc for item in metadata):
                yield where
            return
        yield from _times_without_zone(inner, where, seen)
    elif annotation is datetime:
        yield where
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        for name, field in annotation.model_fields.items():
            # Pydantic moves a field's own Annotated metadata onto the field.
            typed = (
                Annotated[field.annotation, *field.metadata]
                if field.metadata
                else field.annotation
            )
            yield from _times_without_zone(
                typed, f"{annotation.__qualname__}.{name}", seen
            )
    else:
        for arg in get_args(annotation):
            yield from _times_without_zone(arg, where, seen)


def _api_routes(routes: Iterable[BaseRoute]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        # A router included in another one.
        nested = getattr(route, "original_router", None) or getattr(route, "routes", ())
        yield from _api_routes(getattr(nested, "routes", nested))


def test_every_response_time_is_sent_in_utc() -> None:
    seen: set[type] = set()
    bare = {
        f"{', '.join(sorted(route.methods))} {route.path}: {field}"
        for router in [*ROUTERS, *PUBLIC_ROUTERS]
        for route in _api_routes(router.routes)
        if route.response_model is not None
        for field in _times_without_zone(route.response_model, "", seen)
    }
    assert seen, "found no response models to check"
    assert not bare, "declare these UtcDateTime:\n" + "\n".join(sorted(bare))


def test_as_utc_reads_a_time_without_a_zone_as_utc() -> None:
    naive = datetime(2026, 9, 27, 12, 0)
    assert as_utc(naive) == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    east = datetime(2026, 9, 27, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert as_utc(east).tzinfo is UTC
    assert as_utc(east) == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def test_a_utc_time_goes_out_with_z() -> None:
    class Stamp(BaseModel):
        at: UtcDateTime
        maybe: UtcDateTime | None = None

    sent = Stamp(at=datetime(2026, 9, 27, 12, 0, 0, 123456)).model_dump(mode="json")
    assert sent == {"at": "2026-09-27T12:00:00.123456Z", "maybe": None}


def test_a_stored_time_goes_out_with_its_zone() -> None:
    # MySQL hands times back without a zone; sent as they are, a browser
    # shows them hours off.
    stored = datetime(2026, 9, 27, 5, 55, 15)
    sent = OrganizationRead(
        id="o-1",
        name="Acme",
        description="",
        domain="org:o-1",
        created_at=stored,
        created_by="ada",
        updated_at=stored,
        updated_by="ada",
    ).model_dump(mode="json")
    assert sent["created_at"] == "2026-09-27T05:55:15Z"
    assert sent["updated_at"] == "2026-09-27T05:55:15Z"
