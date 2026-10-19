"""``forge-admin-token``: print a bearer token for a user, for local development.

``make web-token`` runs it for the site administrator and sets the result as
the web console's ``VITE_API_TOKEN``. The token names the company groups
they're in (its groups claim), as the identity provider's would: those
``--group`` names, else ``FORGE_ADMIN_LOCAL_USER_GROUPS`` (the local user's),
so permissions linked to them (the site's, and organizations' agent
permissions) are theirs; ``--no-groups`` names none. With ``--organization``
the token acts in one organization (its ``org_id`` claim): what the code
graph's MCP server needs, which serves only that organization's
repositories. See ``auth/tokens.py`` for the token.
"""

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import or_, select

from forge_admin.auth.tokens import TokenError, mint_token
from forge_admin.config import Settings, get_settings
from forge_admin.db.session import create_engine, create_sessionmaker
from forge_admin.models import Organization, User

DEFAULT_DAYS = 90


async def _find(
    settings: Settings, *, msid: str, email: str, organization: str
) -> tuple[User | None, Organization | None]:
    """The user, and the organization named by ID or name (None if none)."""
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session:
            column, value = (User.msid, msid) if msid else (User.email, email)
            user = await session.scalar(select(User).where(column == value))
            found = None
            if organization:
                found = await session.scalar(
                    select(Organization).where(
                        or_(
                            Organization.id == organization,
                            Organization.name == organization,
                        )
                    )
                )
            return user, found
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``forge-admin-token``: print a token for a user."""
    parser = argparse.ArgumentParser(
        prog="forge-admin-token",
        description=(
            "Print a bearer token for a user, signed with FORGE_ADMIN_JWT_SECRET: "
            "for local development, e.g. the web console's VITE_API_TOKEN."
        ),
    )
    who = parser.add_mutually_exclusive_group()
    who.add_argument(
        "--msid", help="the user's MS ID (default: FORGE_ADMIN_SITE_ADMIN_MSID)"
    )
    who.add_argument("--email", help="the user's email address")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        help=(
            "a company group the token says they're in (repeatable): to try "
            "permissions linked to it (default: FORGE_ADMIN_LOCAL_USER_GROUPS)"
        ),
    )
    parser.add_argument(
        "--no-groups",
        action="store_true",
        help="name no company groups, not even FORGE_ADMIN_LOCAL_USER_GROUPS",
    )
    parser.add_argument(
        "--organization",
        help=(
            "the organization the token acts in, by ID or name: the code graph's "
            "MCP server reads only its repositories"
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"days until it expires (default: {DEFAULT_DAYS})",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.jwt_secret is None:
        raise SystemExit("Set FORGE_ADMIN_JWT_SECRET (make env generates one)")
    email = (args.email or "").strip().lower()
    msid = (args.msid or ("" if email else settings.site_admin_msid or "")).lower()
    if not (msid or email):
        raise SystemExit("Pass --msid or --email, or set FORGE_ADMIN_SITE_ADMIN_MSID")
    if args.days < 1:
        raise SystemExit("--days must be at least 1")
    named = (args.organization or "").strip()
    user, organization = asyncio.run(
        _find(settings, msid=msid.strip(), email=email, organization=named)
    )
    if user is None:
        who_text = f"MS ID {msid}" if msid else f"email {email}"
        raise SystemExit(f"No user with {who_text}; add them first (forge-admin-seed)")
    if named and organization is None:
        raise SystemExit(f"No organization with ID or name {named}")
    if args.no_groups and args.group:
        raise SystemExit("Pass --group or --no-groups, not both")
    groups = [] if args.no_groups else (args.group or settings.local_user_groups)
    try:
        token = mint_token(
            settings,
            user,
            lifetime=timedelta(days=args.days),
            groups=groups,
            organization_id=organization.id if organization else None,
        )
    except TokenError as error:
        raise SystemExit(str(error)) from None
    print(token)


if __name__ == "__main__":
    main()
