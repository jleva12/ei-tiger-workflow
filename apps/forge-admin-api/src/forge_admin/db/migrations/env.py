"""Alembic environment: migrates the database named by the forge-admin settings."""

import asyncio
from logging.config import fileConfig

from alembic import context
from forge_task_adk_workflows import run_store
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import forge_admin.models  # noqa: F401  registers every table on Base.metadata
from forge_admin.assistant import ADK_TABLES
from forge_admin.config import get_settings
from forge_admin.db.base import Base
from forge_admin.db.session import UTC_SESSION

config = context.config
# Only the alembic CLI has an ini file; in-process runs keep the app's logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = config.attributes.get("settings") or get_settings()
target_metadata = Base.metadata
# Tables autogenerate leaves alone, which it would otherwise drop: those Google
# ADK creates and migrates itself (the assistant's conversations), and the ADK
# workflow runs', which the run store describes (0005adk_run_store creates
# them as it does).
OTHER_TABLES = ADK_TABLES | set(run_store.metadata.tables)


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """
    Whether autogenerate compares a database object with the models: not
    the tables in ``OTHER_TABLES``.
    """
    return not (type_ == "table" and name in OTHER_TABLES)


def run_migrations_offline() -> None:
    """
    Run migrations in an offline (no database connection) mode.

    This function configures the migration context to operate in an offline mode
    using the specified database URL from the settings and applies migrations by
    generating SQL statements without actually connecting to the database.

    :param None: This function does not accept any parameters.
    :raises: This function does not raise any exceptions.
    :return: None
    """
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        include_name=include_name,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Run database migrations using the provided connection.

    This function configures the migration context with the given connection and
    the target metadata. It starts a transaction to execute all migration steps
    within a consistent state.

    :param connection: The database connection to use for running migrations.
    :type connection: Connection
    :return: None
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Executes database migrations in an online fashion using an asynchronous database engine.

    This function connects to the database using an asynchronous engine
    configured with the provided database URL and ensures that the migration
    procedure is run in a transactional context. After the migration is
    executed, the engine is properly disposed of to release associated resources.

    :return: None
    """
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, connect_args=UTC_SESSION
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
