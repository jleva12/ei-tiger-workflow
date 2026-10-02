"""Structured logging for the Forge apps, uvicorn, FastAPI and every library.

Everything is routed through the stdlib root logger and rendered by structlog, so
third-party and application logs share one format: JSON lines in production (one
object per line, ready for Datadog/Loki/CloudWatch), colored key/value output in
development. Values bound with ``structlog.contextvars`` (e.g. ``request_id``,
bound by :class:`~forge_common.middleware.RequestContextMiddleware`) are attached
to every line, including lines emitted by libraries.

Usage::

    from forge_common.logging import get_logger

    log = get_logger(__name__)
    log.info("order.created", order_id=order.id)

Loggers from ``logging.getLogger(__name__)`` keep working and render the same way.
Needs the ``logging`` extra, ``forge-common[logging]``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Literal

import structlog
from pydantic import BaseModel, field_validator
from structlog.typing import EventDict, Processor

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Loggers that install their own handlers; they're reset to propagate to root.
_MANAGED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")


class LoggingSettings(BaseModel):
    """An app's logging section, e.g. ``FORGE_ADMIN_LOGGING__LEVEL=DEBUG``."""

    # Case-insensitive: info and INFO both work.
    level: LogLevel = "INFO"
    # None picks by where the output goes: "console" on a terminal, "json"
    # everywhere else (containers, pipes, files).
    format: Literal["json", "console"] | None = None
    # Colors in the console format. None picks: off when NO_COLOR is set, on
    # when FORCE_COLOR is set or the output is a terminal. Set it to color
    # output that is piped to a viewer that shows colors (an IDE's or the
    # Claude app's run panel, make up-all-local's prefixed lines).
    colors: bool | None = None
    access_log: bool = True
    # Paths excluded from access logs (health probes are noisy).
    access_log_exclude_paths: list[str] = ["/health", "/health/live", "/health/ready"]
    # Per-logger level overrides for noisy third-party libraries.
    levels: dict[str, LogLevel] = {
        "httpx": "WARNING",
        "httpx2": "WARNING",
        "httpcore": "WARNING",
        # The reloader logs every file event at INFO.
        "watchfiles": "WARNING",
    }

    @field_validator("level", mode="before")
    @classmethod
    def _upper_level(cls, value: object) -> object:
        return _upper(value)

    @field_validator("levels", mode="before")
    @classmethod
    def _upper_levels(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {name: _upper(level) for name, level in value.items()}

    @property
    def resolved_format(self) -> Literal["json", "console"]:
        if self.format:
            return self.format
        return "console" if sys.stdout.isatty() else "json"

    @property
    def resolved_colors(self) -> bool:
        if self.colors is not None:
            return self.colors
        if os.environ.get("NO_COLOR"):
            return False
        return bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty()


def configure_logging(settings: LoggingSettings) -> None:
    json_logs = settings.resolved_format == "json"

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if json_logs:
        shared.append(
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.LINENO,
                },
            )
        )
    # Stdlib records (uvicorn, saq, ...) also carry any `extra={...}` fields.
    foreign_pre_chain: list[Processor] = [
        *shared,
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    final: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if json_logs:
        final += [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    else:
        final.append(structlog.dev.ConsoleRenderer(colors=settings.resolved_colors))

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain, processors=final
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.level)

    for name in _MANAGED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    # Access lines come from RequestContextMiddleware (with request_id and duration).
    logging.getLogger("uvicorn.access").disabled = True

    for name, level in settings.levels.items():
        logging.getLogger(name).setLevel(level)

    logging.captureWarnings(True)


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


def _drop_color_message(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    # Uvicorn attaches an ANSI-colored duplicate of each message as `extra`.
    event_dict.pop("color_message", None)
    return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
