"""
How an ADK workflow run was started, for its audit
(``forge_task_adk_workflows.run_store.Provenance``), from what started it:

- The admin API's run route (:func:`console_start`): the assistant's tools
  (their token says the assistant acts for the person), an organization's API
  key, the builder's test panel (``X-Forge-Client: builder``), Forge's console
  otherwise (``X-Forge-Client: console``), or a person's own call to the API.
  Resubmitting a run is told apart the same way.
- The runtime's workflow API and A2A (:func:`runtime_start`): its caller, a
  person, a key, or anonymous (a public runtime's).
- A chat agent's Workflow tool, or an intake it completed
  (:func:`chat_start`): for the conversation's person, verified only when
  they're who called (not an ID a public runtime's caller, an API key's end
  user or a chat page's visitor passed on).

Each keeps where the request came from and how its caller signed in
(``forge_admin.auth.origin``). ``X-Forge-Client`` is what the client says it
is: it tells the console's starts apart, and vouches for nobody.
"""

from dataclasses import replace

from fastapi import Request
from forge_task_adk_workflows.run_store import (
    A2A,
    API_KEY,
    ASSISTANT,
    BUILDER_TEST,
    CHAT_AGENT,
    INTAKE,
    MANUAL,
    RUNTIME_REST,
    Actor,
    Provenance,
)

from forge_admin.auth.api_keys import key_id_of
from forge_admin.auth.origin import API_KEY as KEY_AUTH
from forge_admin.auth.origin import JWT, RequestOrigin, current_origin
from forge_admin.overview.recording import API, CLIENT_HEADER, CONSOLE

#: What the builder's test panel says it is (``X-Forge-Client``).
BUILDER = "builder"
#: Through what a run came in (``channel``), besides the turn channels.
ASSISTANT_CHANNEL, WORKER = "assistant", "worker"
#: Who acts for a person in the assistant's tokens (``user_tokens.ASSISTANT``).
ASSISTANT_ACTOR = "assistant"


def origin_of_request(request: Request) -> RequestOrigin:
    """:return: Where the request came from and how its caller signed in."""
    found = getattr(request.state, "origin", None)
    return found if isinstance(found, RequestOrigin) else current_origin()


def _verified(origin: RequestOrigin) -> bool:
    """:return: Whether the caller's own credential said who they are."""
    return origin.auth_method in (JWT, KEY_AUTH)


def _from(origin: RequestOrigin, provenance: Provenance) -> Provenance:
    """:return: How it was started, from where the request came, signed in so."""
    return replace(
        provenance,
        auth_method=origin.auth_method,
        client_ip=origin.client_ip,
        user_agent=origin.user_agent,
        request_id=origin.request_id,
    )


def console_start(request: Request, starter: Actor) -> Provenance:
    """
    :param starter: Who called the admin API's route: a person, or a key.
    :return: How a start (or resubmission) on the admin API came: the
        assistant's, an API key's, the builder's test panel's, the console's,
        or a person's call to the API.
    """
    origin = origin_of_request(request)
    key = key_id_of(starter.id)
    client = request.headers.get(CLIENT_HEADER)
    if origin.actor == ASSISTANT_ACTOR:
        trigger, channel = ASSISTANT, ASSISTANT_CHANNEL
    elif key is not None:
        trigger, channel = API_KEY, API
    elif client == BUILDER:
        trigger, channel = BUILDER_TEST, CONSOLE
    else:
        trigger, channel = MANUAL, CONSOLE if client == CONSOLE else API
    started = Provenance(
        trigger,
        channel=channel,
        on_behalf_of=starter.id,
        on_behalf_of_name=starter.name,
        on_behalf_of_verified=_verified(origin),
        api_key_id=key,
    )
    return _from(origin, started)


def runtime_start(
    request: Request, caller: str | None, starter: Actor, *, protocol: str
) -> Provenance:
    """
    :param caller: Who called, as their credential says: a person,
        ``apikey:<id>``, or None (anonymous).
    :param starter: Whom the run is for: the caller, or ``anonymous``.
    :param protocol: ``rest`` or ``a2a``.
    :return: How a start on the runtime's workflow API or A2A came.
    """
    origin = origin_of_request(request)
    a2a = protocol == "a2a"
    started = Provenance(
        A2A if a2a else RUNTIME_REST,
        channel="a2a" if a2a else API,
        on_behalf_of=starter.id,
        on_behalf_of_name=starter.name,
        on_behalf_of_verified=caller is not None and _verified(origin),
        api_key_id=key_id_of(caller),
    )
    return _from(origin, started)


def chat_start(
    *,
    user_id: str,
    user_name: str,
    caller: str | None,
    channel: str | None,
    agent_id: str,
    agent_version: str | None,
    intake: bool,
) -> Provenance:
    """
    :param user_id: Whose conversation it is, as the call says.
    :param user_name: Their name, when it's known.
    :param caller: Who called the agent, as their credential says: a
        person, ``apikey:<id>``, or None (anonymous, a chat page's visitor).
    :param channel: How the turn came in (``overview.recording``).
    :param agent_id: The chat agent.
    :param agent_version: Which version of it ran.
    :param intake: An intake it ran started it (completing), not its
        Workflow tool.
    :return: How a chat agent's start came: for the conversation's person,
        verified only when they're who called.
    """
    origin = current_origin()
    started = Provenance(
        INTAKE if intake else CHAT_AGENT,
        channel=channel or API,
        on_behalf_of=user_id,
        on_behalf_of_name=user_name,
        on_behalf_of_verified=caller is not None
        and user_id == caller
        and _verified(origin),
        via_agent_id=agent_id,
        via_agent_version=agent_version,
        api_key_id=key_id_of(caller),
    )
    return _from(origin, started)
