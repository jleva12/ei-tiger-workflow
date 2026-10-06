"""The hosted runtime's routes: ``forge_agent_runtime``'s run API and A2A, its
agents the organizations' chat agents (``source.MongoAgentSource``).

Mounted at ``{api_prefix}/runtime``:

- ADK's run API: ``POST /runtime/run_sse`` with ``appName`` ``ca_x`` (latest
  published), ``ca_x@3`` or ``ca_x@draft``, and the sessions under
  ``/runtime/apps/{app}/users/{user}/sessions``.
- Google's A2A protocol (JSON-RPC, 1.0 and 0.3): ``POST /runtime/a2a/{app}``,
  and each agent's card at ``/runtime/a2a/{app}/.well-known/agent-card.json``.
  A2A conversations are the run API's sessions; tasks are kept in the admin
  MySQL (``a2a_tasks``, 0010a2a).

Public unless ``agent_runtime_public`` is false (``api.server``): then both
ask for an organization's API key or a Forge sign-in, holding agents:run in
the organization the agent is theirs (``forge_admin.auth.runtime_access``),
and a person's conversations are their own. A2A tasks are each caller's own
whenever the caller is known.
"""

from a2a.server.tasks import TaskStore
from a2a.types import SecurityScheme
from fastapi import APIRouter, HTTPException, Request, status
from forge_agent_runtime import AgentExecutor
from forge_agent_runtime.a2a import api_key_security, create_a2a_router, known_caller
from forge_agent_runtime.executor import ResolvedAgent
from forge_agent_runtime.server import CallAction, create_router

from forge_admin.adk_workflows.a2a import workflow_a2a_service
from forge_admin.auth.runtime_access import authorize_call, check_conversation_user
from forge_admin.config import Settings

NOT_SET_UP = "Chat agents aren't set up: set FORGE_ADMIN_MONGO_URI"


def executor_of(request: Request) -> AgentExecutor:
    """:raises HTTPException: 503 when chat agents aren't set up (no MongoDB)."""
    executor: AgentExecutor | None = getattr(request.app.state, "agent_executor", None)
    if executor is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    return executor


def tasks_of(request: Request) -> TaskStore:
    """:return: Where A2A tasks are kept, which the app makes as it starts."""
    tasks: TaskStore | None = getattr(request.app.state, "a2a_tasks", None)
    if tasks is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    return tasks


async def authorize_agent(
    request: Request,
    agent: ResolvedAgent,
    *,
    action: CallAction,
    user_id: str | None,
) -> None:
    """
    Whether a request may call an agent: anyone when the runtime is public;
    otherwise a caller with agents:run in the agent's organization, and
    only into conversations of their own.

    :raises HTTPException: 401 without a caller; 403 otherwise refused.
    """
    caller = await authorize_call(request, agent.document.organization_id)
    if user_id is not None:
        await check_conversation_user(request, caller, user_id)


def runtime_security() -> dict[str, SecurityScheme]:
    """How callers sign in, for the cards when the runtime isn't public."""
    return api_key_security(
        bearer=(
            "An organization's API key (fk_…), or a Forge sign-in token: "
            "Authorization: Bearer <it>"
        ),
        header="An organization's API key (fk_…)",
    )


router = create_router(executor_of, authorize=authorize_agent)


def a2a_router(settings: Settings) -> APIRouter:
    """
    :return: The A2A routes, their cards saying how to sign in when the
        runtime isn't public, and where it is (``agent_runtime_url``); the
        organizations' workflows (``ag_…``) beside their chat agents.
    """
    security = None if settings.agent_runtime_public else runtime_security()
    return create_a2a_router(
        executor_of,
        path="/a2a",
        tasks=tasks_of,
        # A known caller's tasks are their own: a person's, or a key's.
        caller=known_caller,
        security=security,
        base_url=settings.agent_runtime_url,
        authorize=authorize_agent,
        services=[workflow_a2a_service(security=security)],
    )
