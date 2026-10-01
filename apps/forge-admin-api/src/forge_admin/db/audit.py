"""Who and when for the audit columns every table carries.

The actor is held in a context variable, set once per request by the security
dependency, and read by the column defaults when SQLAlchemy writes a row. Code
outside a request (migrations, scripts) writes as ``system``.
"""

from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

SYSTEM_ACTOR = "system"

_actor: ContextVar[str] = ContextVar("forge_admin_actor", default=SYSTEM_ACTOR)


def current_actor() -> str:
    """
    Return who is making the current change.

    :return: The actor set for this request, or ``system`` outside one.
    """
    return _actor.get()


def set_actor(actor: str) -> None:
    """
    Record who is making changes for the rest of the current request.

    :param actor: A user or service identifier, at most 255 characters.
    """
    _actor.set(actor[:255])


def utc_now() -> datetime:
    """
    Return the current UTC time as the naive datetime MySQL stores.

    :return: The current time in UTC, without a timezone.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    """
    The same instant in UTC, taking a time without a zone to be UTC already.

    :param value: A time from MySQL, which keeps them without a zone and is
        written in UTC, or one with a zone.
    :return: The time with a UTC zone.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# A time in a response. It always goes out with its zone (``Z``): clients read
# a time without one as their own local time.
UtcDateTime = Annotated[datetime, AfterValidator(as_utc)]
