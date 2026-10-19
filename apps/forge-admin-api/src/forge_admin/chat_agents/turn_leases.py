"""The hosted runtime's turn leases (``forge_agent_runtime.leases``), kept in
the admin database: every process serving chat agents shares them, so a
conversation's turns run one at a time across them all.

A lease is a row per conversation (``chat_turn_leases``), taken by updating
it when it ran out (or is the taker's) and inserting it when there's none:
two processes racing for it both find it free, and one insert fails. It's
renewed only by its owner, and given back by deleting it.
"""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta

from forge_agent_runtime.leases import TurnKey
from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge_admin.db.audit import utc_now
from forge_admin.models import ChatTurnLease


def lease_id(key: TurnKey) -> str:
    """:return: A conversation's lease's row: SHA-256 of its agent, person and session."""
    return hashlib.sha256(
        f"{key.app_name}\n{key.user_id}\n{key.session_id}".encode()
    ).hexdigest()


class SqlTurnLeases:
    """Conversations' turn leases in the admin database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sessions = sessions
        self.clock = clock

    async def acquire(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        now = self.clock()
        until = now + timedelta(seconds=seconds)
        row = lease_id(key)
        async with self.sessions() as session:
            taken = await session.execute(
                update(ChatTurnLease)
                .where(
                    ChatTurnLease.id == row,
                    or_(ChatTurnLease.expires_at < now, ChatTurnLease.owner == owner),
                )
                .values(owner=owner, expires_at=until)
            )
            if taken.rowcount:  # type: ignore[attr-defined]
                await session.commit()
                return True
            session.add(
                ChatTurnLease(
                    id=row,
                    app_name=key.app_name[:64],
                    user_id=key.user_id[:255],
                    session_id=key.session_id[:128],
                    owner=owner,
                    expires_at=until,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # Held, or another turn took it first.
                await session.rollback()
                return False
            return True

    async def renew(self, key: TurnKey, *, owner: str, seconds: float) -> bool:
        async with self.sessions() as session:
            renewed = await session.execute(
                update(ChatTurnLease)
                .where(ChatTurnLease.id == lease_id(key), ChatTurnLease.owner == owner)
                .values(expires_at=self.clock() + timedelta(seconds=seconds))
            )
            await session.commit()
            return bool(renewed.rowcount)  # type: ignore[attr-defined]

    async def release(self, key: TurnKey, *, owner: str) -> None:
        async with self.sessions() as session:
            await session.execute(
                delete(ChatTurnLease).where(
                    ChatTurnLease.id == lease_id(key), ChatTurnLease.owner == owner
                )
            )
            await session.commit()
