"""The services a run's steps use, built once per worker process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from forge_task_workflows.config import WorkflowsSettings
from forge_task_workflows.errors import NotSetUp
from forge_task_workflows.services.admin import AdminClient
from forge_task_workflows.services.llm import LanguageModels


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Services:
    settings: WorkflowsSettings
    http: Any  # an httpx.AsyncClient for HTTP steps
    admin: AdminClient | None = None
    llm: LanguageModels | None = None
    # Why there are no models, when their configuration couldn't be read.
    llm_unavailable: str | None = None
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def require_admin(self) -> AdminClient:
        if self.admin is None:
            raise NotSetUp(
                "The worker can't reach the admin API: set HYBRID_WORKFLOWS__ADMIN_URL and "
                "HYBRID_WORKFLOWS__ADMIN_TOKEN (FORGE_WORKFLOWS_TOKEN)"
            )
        return self.admin

    def require_llm(self) -> LanguageModels:
        if self.llm is None:
            raise NotSetUp(
                f"Agent steps aren't set up here: {self.llm_unavailable}"
                if self.llm_unavailable
                else "Agent steps aren't set up here: set HYBRID_WORKFLOWS__MODEL_PROVIDER_CONFIG, "
                "or HYBRID_WORKFLOWS__GOOGLE_API_KEY (FORGE_GOOGLE_API_KEY)"
            )
        return self.llm
