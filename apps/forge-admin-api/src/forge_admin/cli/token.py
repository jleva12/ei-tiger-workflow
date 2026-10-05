"""``forge-admin-token``: print a bearer token for a user, for local development.

``make web-token`` runs it for the site administrator and sets the result as
the web console's ``VITE_API_TOKEN``. See ``auth/tokens.py`` for the token.
"""

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import select

from forge_admin.auth.tokens import mint_token
from forge_admin.config import Settings, get_settings
from forge_admin.db.session import create_engine, create_sessionmaker
from forge_admin.models import User

DEFAULT_DAYS = 90


async def _find_user(settings: Settings, *, msid: str, email: str) -> User | None:
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session:
            column, value = (User.msid, msid) if msid else (User.email, email)
            return await session.scalar(select(User).where(column == value))
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
            "permissions linked to it"
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
    user = asyncio.run(_find_user(settings, msid=msid.strip(), email=email))
    if user is None:
        who_text = f"MS ID {msid}" if msid else f"email {email}"
        raise SystemExit(f"No user with {who_text}; add them first (forge-admin-seed)")
    print(
        mint_token(
            settings, user, lifetime=timedelta(days=args.days), groups=args.group
        )
    )


if __name__ == "__main__":
    main()
