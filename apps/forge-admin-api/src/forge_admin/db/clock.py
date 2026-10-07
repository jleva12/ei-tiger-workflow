"""The database's own clock, for times another process compares with it."""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import DateTime


class database_now(FunctionElement[datetime]):  # noqa: N801 (a SQL function)
    """
    Now, in UTC, by the database's clock, to the microsecond: for a time a
    process on another host compares with the database's clock, such as when
    the code graph worker may claim a job, which this host's clock would skew.
    ``UTC_TIMESTAMP(6)`` on MySQL; ``CURRENT_TIMESTAMP`` (UTC) elsewhere.
    """

    type = DateTime()
    inherit_cache = True


@compiles(database_now, "mysql")
def _mysql(element: database_now, compiler: SQLCompiler, **kw: Any) -> str:
    return "UTC_TIMESTAMP(6)"


@compiles(database_now)
def _default(element: database_now, compiler: SQLCompiler, **kw: Any) -> str:
    return "CURRENT_TIMESTAMP"
