"""Seed: the first user, the site administrator, who can then add everyone else.

Run ``forge-admin-seed`` (``make infrastructure`` runs it in Docker). It first
applies migrations, which create the default roles and permissions, then adds
the site administrator described by the ``FORGE_ADMIN_SITE_ADMIN_*`` settings
to users and gives them the ``site:admin`` role on the whole site. They then
sign in with a bearer token (``forge-admin-token``). Running it again changes
nothing.
"""

import asyncio
import logging

from forge_common.logging import configure_logging
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.api.routes.users import UserCreate
from forge_admin.auth.authorization import find_assignment
from forge_admin.config import Settings, get_settings
from forge_admin.db.audit import set_actor
from forge_admin.db.migrate import upgrade
from forge_admin.db.session import create_engine, create_sessionmaker
from forge_admin.models import CasbinRule, Role, User

logger = logging.getLogger(__name__)

SITE_ADMIN_ROLE = "site:admin"
SITE_PATTERN = "*"
SEED_ACTOR = "seed"
# Setting (FORGE_ADMIN_SITE_ADMIN_<NAME>) for each field of the user.
SITE_ADMIN_SETTINGS = {
    "id": "site_admin_id",
    "first_name": "site_admin_first_name",
    "last_name": "site_admin_last_name",
    "email": "site_admin_email",
    "msid": "site_admin_msid",
}


async def add_user(session: AsyncSession, admin: UserCreate) -> bool:
    """
    Add the site administrator to users, unless they're there already.

    :param session: A database session; the change is committed.
    :param admin: The user, with their ID.
    :return: Whether they were added now (False if already there).
    :raises RuntimeError: Another user has their email or MS ID.
    """
    if await session.get(User, admin.id) is not None:
        return False
    other = await session.scalar(
        select(User.id).where(or_(User.email == admin.email, User.msid == admin.msid))
    )
    if other is not None:
        raise RuntimeError(
            f"User {other} already has the email {admin.email} or MS ID {admin.msid}"
        )
    session.add(User(**admin.model_dump()))
    await session.commit()
    return True


async def make_site_admin(session: AsyncSession, admin_id: str) -> bool:
    """
    Assign ``site:admin`` on the site to a user, if needed.

    :param session: A database session; the change is committed.
    :param admin_id: The user's ID.
    :return: Whether the role was assigned now (False if already held).
    :raises RuntimeError: The site:admin role does not exist.
    """
    if await session.get(Role, SITE_ADMIN_ROLE) is None:
        raise RuntimeError(
            f"The {SITE_ADMIN_ROLE} role is missing; its migration creates it"
        )
    if await find_assignment(session, admin_id, SITE_ADMIN_ROLE, SITE_PATTERN):
        return False
    session.add(CasbinRule(ptype="g", v0=admin_id, v1=SITE_ADMIN_ROLE, v2=SITE_PATTERN))
    await session.commit()
    return True


async def seed(session: AsyncSession, admin: UserCreate) -> bool:
    """
    Add the site administrator to users and give them ``site:admin``.

    :param session: A database session; the changes are committed.
    :param admin: The user, with their ID.
    :return: Whether anything changed.
    """
    added = await add_user(session, admin)
    assigned = await make_site_admin(session, admin.id or "")
    return added or assigned


def site_admin(settings: Settings) -> UserCreate:
    """
    Read the site administrator from the ``FORGE_ADMIN_SITE_ADMIN_*`` settings.

    :param settings: The settings.
    :return: The user, validated and normalized as the users API would.
    :raises SystemExit: A setting is missing or not valid.
    """
    values = {
        field: getattr(settings, name) for field, name in SITE_ADMIN_SETTINGS.items()
    }
    missing = [
        f"FORGE_ADMIN_{name.upper()}"
        for field, name in SITE_ADMIN_SETTINGS.items()
        if not values[field]
    ]
    if missing:
        raise SystemExit(f"Set the site administrator: {', '.join(missing)}")
    try:
        return UserCreate.model_validate(values)
    except ValidationError as error:
        raise SystemExit(f"Invalid site administrator settings:\n{error}") from None


async def _run(admin: UserCreate) -> bool:
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as session:
            return await seed(session, admin)
    finally:
        await engine.dispose()


def main() -> None:
    """Entry point for ``forge-admin-seed``: migrate, then seed."""
    settings = get_settings()
    configure_logging(settings.logging)
    admin = site_admin(settings)
    upgrade(settings)
    set_actor(SEED_ACTOR)
    changed = asyncio.run(_run(admin))
    logger.info(
        "seeded: %s %s (%s, MS ID %s) %s %s on the site",
        admin.first_name,
        admin.last_name,
        admin.id,
        admin.msid,
        "is now" if changed else "already was",
        SITE_ADMIN_ROLE,
    )


if __name__ == "__main__":
    main()
