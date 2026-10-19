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

Callers send an organization's API key or a Forge sign-in, holding agents:run
in the organization the agent is theirs (``forge_admin.auth.runtime_access``),
and are let in by the agent's ID and organization before anything of it is
read: another organization's agent is one there isn't. A draft
(``ca_x@draft``) is for those who edit the organization's agents
(agents:manage). A person's conversations are their own, and a key's are its
end users', apart from every other key's (``conversation_owner``); A2A tasks
are each caller's own. Each of an agent's agents with a rule on its node
answers only those holding what it needs, telling the others what that is
(``forge_admin.chat_agents.access``): the runtime checks it as the agent
runs, and the chat agent's as each turn starts, for the caller recorded here.
"""

from a2a.server.tasks import TaskStore
from a2a.types import SecurityScheme
from fastapi import APIRouter, HTTPException, Request, status
from forge_agent_runtime import AgentExecutor
from forge_agent_runtime.a2a import api_key_security, create_a2a_router, known_caller
from forge_agent_runtime.executor import LocatedAgent, ResolvedAgent
from forge_agent_runtime.server import CallAction, create_router

from forge_admin.adk_workflows.a2a import workflow_a2a_service
from forge_admin.auth.runtime_access import (
    MANAGE,
    RUN,
    admit_call,
    caller_of,
    conversation_owner,
)
from forge_admin.chat_agents.access import (
    ANONYMOUS,
    AgentUser,
    agent_user,
    set_agent_user,
)
from forge_admin.config import Settings
from forge_admin.overview.recording import A2A, API, CLIENT_HEADER, CONSOLE, set_channel

NOT_SET_UP = "Chat agents aren't set up: set FORGE_ADMIN_MONGO_URI"
UNAVAILABLE = "The agents' database isn't answering; try again shortly"


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


async def admit_agent(
    request: Request,
    agent: LocatedAgent,
    *,
    action: CallAction,
) -> None:
    """
    Whether a request may call an agent, by its ID and organization alone,
    before anything of it is read: a caller with agents:run in its
    organization (agents:manage for its draft). Anyone without standing
    there is told there's no such agent.

    :raises HTTPException: 401 without a caller; 403 otherwise refused; 404
        for another organization's agent.
    """
    draft = agent.ref.version == "draft"
    await admit_call(
        request, agent.organization_id, permission=MANAGE if draft else RUN
    )


async def authorize_agent(
    request: Request,
    agent: ResolvedAgent,
    *,
    action: CallAction,
    user_id: str | None,
) -> None:
    """
    Record who a call of an agent, once let in, is for, and how its turns
    came in.

    :raises HTTPException: 404 when the agent found isn't of the
        organization the caller was let in to.
    """
    caller = caller_of(request)
    if (
        caller.organization_id is not None
        and caller.organization_id != agent.document.organization_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such agent")
    await authorize_use(request, agent, caller.subject, caller.groups)
    # How its turns came in, for the conversation's audit: A2A, the builder's
    # chat (a signed-in person in Forge's console), or the run API.
    if action == "a2a":
        set_channel(A2A)
    elif action == "run":
        console = request.headers.get(CLIENT_HEADER) == CONSOLE
        person = not (caller.is_anonymous or caller.is_api_key)
        set_channel(CONSOLE if console and person else API)


async def authorize_use(
    request: Request,
    agent: ResolvedAgent,
    subject: str | None,
    groups: tuple[str, ...] = (),
) -> AgentUser:
    """
    Record who a call of an agent is for: the rules on its agents' nodes
    are checked for them as each agent runs, and so are the runs of its
    workflow tools.

    :param subject: Who's calling: a user's ID, an API key's, or None.
    :param groups: The company groups their sign-in names.
    :return: Who's using it, and whether they manage the organization's
        agents (who may use them all).
    """
    organization_id = agent.document.organization_id
    if organization_id is None or subject is None:
        user = AgentUser(subject, groups) if subject is not None else ANONYMOUS
    else:
        user = await agent_user(
            request.app.state.enforcer, organization_id, subject, groups
        )
    set_agent_user(user)
    return user


def runtime_security() -> dict[str, SecurityScheme]:
    """How callers sign in, for the cards when the runtime isn't public."""
    return api_key_security(
        bearer=(
            "An organization's API key (fk_…), or a Forge sign-in token: "
            "Authorization: Bearer <it>"
        ),
        header="An organization's API key (fk_…)",
    )


router = create_router(
    executor_of,
    admit=admit_agent,
    authorize=authorize_agent,
    owner=conversation_owner,
)


def a2a_router(settings: Settings) -> APIRouter:
    """
    :return: The A2A routes, their cards saying how to sign in and where
        they are (``agent_runtime_url``); the organizations' workflows
        (``ag_…``) beside their chat agents.
    """
    security = runtime_security()
    return create_a2a_router(
        executor_of,
        path="/a2a",
        tasks=tasks_of,
        # A known caller's tasks are their own: a person's, or a key's.
        caller=known_caller,
        security=security,
        base_url=settings.agent_runtime_url,
        admit=admit_agent,
        authorize=authorize_agent,
        services=[workflow_a2a_service(security=security)],
    )
