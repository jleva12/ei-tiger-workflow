"""Admin tables on SQLite, for tests without MySQL: as their models make
them, without MySQL's server defaults (``CURRENT_TIMESTAMP(6)``), which
SQLite doesn't take; their models fill those in themselves."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine


def on_sqlite(*tables: Any) -> list[sa.Table]:
    """:return: The models' tables, as SQLite takes them."""
    meta = sa.MetaData()
    made = []
    for model in tables:
        table: sa.Table = model.__table__
        made.append(
            sa.Table(
                table.name,
                meta,
                *(
                    sa.Column(
                        column.name,
                        column.type,
                        primary_key=column.primary_key,
                        nullable=column.nullable,
                    )
                    for column in table.columns
                ),
            )
        )
    return made


async def create(engine: AsyncEngine, *models: Any) -> None:
    tables = on_sqlite(*models)
    async with engine.begin() as conn:
        for table in tables:
            await conn.run_sync(table.create)
