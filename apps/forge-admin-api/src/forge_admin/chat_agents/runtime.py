"""The hosted runtime's routes: ``forge_agent_runtime``'s run API, its agents
the organizations' chat agents (``source.MongoAgentSource``).

Mounted at ``{api_prefix}/runtime``: ``POST /runtime/run_sse`` with
``appName`` ``ca_x`` (latest published), ``ca_x@3`` or ``ca_x@draft``, and the
sessions under ``/runtime/apps/{app}/users/{user}/sessions``. Public unless
``agent_runtime_public`` is false (``api.server``).
"""

from fastapi import HTTPException, Request, status
from forge_agent_runtime import AgentExecutor
from forge_agent_runtime.server import create_router


def executor_of(request: Request) -> AgentExecutor:
    """:raises HTTPException: 503 when chat agents aren't set up (no MongoDB)."""
    executor: AgentExecutor | None = getattr(request.app.state, "agent_executor", None)
    if executor is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Chat agents aren't set up: set FORGE_ADMIN_MONGO_URI",
        )
    return executor


router = create_router(executor_of)
