"""The declarative bases every model derives from, and shared column types."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Dialect, MetaData, String, Text, text
from sqlalchemy.dialects import mysql
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

from forge_admin.db.audit import SYSTEM_ACTOR, current_actor, utc_now

# Deterministic constraint names, so migrations can alter and drop them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def ascii_string(length: int) -> String:
    """
    A case-sensitive ASCII column on MySQL, for identifiers and keys.

    ASCII also keeps multi-column indexes well within MySQL's key size limit.

    :param length: Maximum length in characters.
    :return: The column type.
    """
    return String(length).with_variant(
        mysql.VARCHAR(length, charset="ascii", collation="ascii_bin"), "mysql"
    )


class JSONText(TypeDecorator[Any]):
    """
    A JSON value kept as text, exactly as written: MySQL's JSON type sorts
    object keys, which loses an order people chose, such as a schema's
    properties, or a payload's as it was sent. MEDIUMTEXT (16 MiB) on MySQL.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        return dialect.type_descriptor(
            mysql.MEDIUMTEXT() if dialect.name == "mysql" else Text()
        )

    def process_bind_param(self, value: Any, dialect: Dialect) -> str:
        # JSON null too, as the text "null".
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        return None if value is None else json.loads(value)

    def compare_values(self, x: Any, y: Any) -> bool:
        # The order counts: reordering a value's keys is a change to save.
        return json.dumps(x) == json.dumps(y)


# Microsecond precision, so rows written in the same second still order.
AUDIT_TIMESTAMP = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
AUDIT_TIMESTAMP_DEFAULT = text("CURRENT_TIMESTAMP(6)")


def _created_at_of_new_row(context: DefaultExecutionContext) -> datetime:
    # Defaults are computed in column order, so created_at is already set;
    # reusing it makes a never-updated row's two timestamps identical.
    created_at = context.get_current_parameters().get("created_at")
    return created_at if isinstance(created_at, datetime) else utc_now()


class AuditBase(Base):
    """
    Base class for every table: who created and last updated each row, and when.

    The values are filled in automatically on every insert and update made
    through SQLAlchemy, including bulk ``update()`` statements, from
    :func:`forge_admin.db.audit.current_actor` and the UTC clock. The server
    defaults cover rows written outside SQLAlchemy. The columns sort last in
    each table.
    """

    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(
        AUDIT_TIMESTAMP,
        default=utc_now,
        server_default=AUDIT_TIMESTAMP_DEFAULT,
        sort_order=100,
    )
    created_by: Mapped[str] = mapped_column(
        String(255),
        default=current_actor,
        server_default=SYSTEM_ACTOR,
        sort_order=100,
    )
    updated_at: Mapped[datetime] = mapped_column(
        AUDIT_TIMESTAMP,
        default=_created_at_of_new_row,
        onupdate=utc_now,
        server_default=AUDIT_TIMESTAMP_DEFAULT,
        sort_order=100,
    )
    updated_by: Mapped[str] = mapped_column(
        String(255),
        default=current_actor,
        onupdate=current_actor,
        server_default=SYSTEM_ACTOR,
        sort_order=100,
    )
