"""The SQLAlchemy engine and the per-request database sessions."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forge_admin.config import Settings

# Every connection works in UTC. The columns are DATETIME, which keeps no
# zone, and hold UTC; CURRENT_TIMESTAMP and NOW(), which fill the server
# defaults, follow the session's zone, the server's own unless set here.
UTC_SESSION = {"init_command": "SET time_zone = '+00:00'"}


def create_engine(settings: Settings) -> AsyncEngine:
    """
    Creates and configures an asynchronous database engine for use with MySQL. This function
    utilizes the provided settings to define connection parameters and pooling behavior,
    ensuring the engine efficiently manages database connections while maintaining
    reliability.

    :param settings: Configuration object containing database connection parameters and
        pooling settings.
    :type settings: Settings
    :return: An instance of AsyncEngine configured based on the provided settings.
    :rtype: AsyncEngine
    """
    return create_async_engine(
        settings.database_url,
        pool_size=settings.mysql_pool_size,
        max_overflow=settings.mysql_max_overflow,
        pool_recycle=settings.mysql_pool_recycle,
        # Replace connections MySQL closed while they sat in the pool.
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.mysql_connect_timeout, **UTC_SESSION},
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """
    Creates and configures an asynchronous sessionmaker instance using the provided engine.

    This function is used to generate an asynchronous sessionmaker, which is a factory for
    producing database session objects. It binds the provided asynchronous engine to the
    sessionmaker and configures it so that objects are not automatically expired after
    each commit operation. This ensures that the local state of objects remains intact even
    after committing changes to the database.

    :param engine: An instance of an asynchronous database engine (e.g., AsyncEngine)
        to be used for connecting to the database.
    :return: An instance of async_sessionmaker configured with the provided engine and
        settings for session creation.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> None:
    """
    Ping the database to verify connectivity.

    This function attempts to establish a connection to the database and execute a
    simple query to confirm that the database is reachable and operational.

    :param engine: The asynchronous SQLAlchemy database engine used to make the
        connection.
    :return: None
    """
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """
    Provides an asynchronous generator function to retrieve a database session for handling
    requests in a web application.

    :param request: The HTTP request object. It contains contextual information about the incoming
        HTTP request and is used to access the application's state, which holds the sessionmaker
        instance to create database sessions.
    :return: An asynchronous iterator that yields an instance of AsyncSession. This session
        is used to interact with the database and is automatically managed by the context manager.
    """
    async with request.app.state.sessionmaker() as session:
        yield session
