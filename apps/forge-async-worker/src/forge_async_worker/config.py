"""The worker's settings: what every task shares
(:class:`~forge_tasks.settings.CoreSettings`: ``HYBRID_ENABLED_TASKS``,
``HYBRID_REDIS_URL``, ``HYBRID_MONGO__*``), where it tracks runs
(``HYBRID_ETF__*``), its background tasks API (``HYBRID_API__*``) and its
logs (``HYBRID_LOGGING__*``). Each
task package reads its own section (``HYBRID_WORKFLOWS__*``, ...), so none of
that is configured here.
"""

from __future__ import annotations

from typing import Literal

from forge_common.logging import LoggingSettings
from pydantic import BaseModel, SecretStr

from forge_tasks.settings import CoreSettings


class EtfSettings(BaseModel):
    """The enhanced task framework's run tracking."""

    # "mongo": runs, steps and the audit trail in ``database`` on HYBRID_MONGO__URI,
    # with Mongo leases. "memory": in-process only (tests, one-off runs).
    store: Literal["mongo", "memory"] = "mongo"
    database: str = "forge_tasks"
    # The maintenance sweep: runs untouched this long whose lease is free are
    # recovered (their worker died) and re-driven; approvals past their deadline
    # expire.
    stale_after_seconds: int = 900
    maintenance_cron: str = "*/5 * * * *"


class ApiSettings(BaseModel):
    """The background tasks API (``forge-async-worker api``), which the admin API
    calls to show an organization its tasks and act on them. Internal: every request
    needs ``token`` as its bearer token, and the API won't start without one."""

    token: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8104


class WorkerSettings(CoreSettings):
    etf: EtfSettings = EtfSettings()
    api: ApiSettings = ApiSettings()
    # Structured logs (forge_common.logging): HYBRID_LOGGING__LEVEL, __FORMAT
    # (json or console; unset, console on a terminal and json otherwise),
    # __ACCESS_LOG, __ACCESS_LOG_EXCLUDE_PATHS and __LEVELS.
    logging: LoggingSettings = LoggingSettings()
