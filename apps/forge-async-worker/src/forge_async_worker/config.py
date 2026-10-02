"""The worker's settings: what every task shares
(:class:`~forge_tasks.settings.CoreSettings`: ``HYBRID_ENABLED_TASKS`` and
``HYBRID_REDIS_URL``, the Redis of the ``adk_workflows`` queue) and its logs
(``HYBRID_LOGGING__*``). Each task package reads its own section
(``HYBRID_ADK_WORKFLOWS__*``), so none of that is configured here; the run
store is in the ADK workflows task's session database
(``HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL``, the admin MySQL).
"""

from __future__ import annotations

from forge_common.logging import LoggingSettings

from forge_tasks.settings import CoreSettings


class WorkerSettings(CoreSettings):
    # Structured logs (forge_common.logging): HYBRID_LOGGING__LEVEL, __FORMAT
    # (json or console; unset, console on a terminal and json otherwise) and
    # __LEVELS.
    logging: LoggingSettings = LoggingSettings()
