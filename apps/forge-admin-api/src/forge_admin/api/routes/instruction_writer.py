"""
The instruction writer, for the builders: the Agents canvas's agents and a
workflow's LLM agents. The person says what the agent should do; the writer
(``forge_admin.instruction_writer``) writes its instruction for the agent as
it's set up, from the brief the builder sends, filled in from the
organization's MCP servers, knowledge bases, agents and workflows.

Needs ``agents:manage`` in the organization: it's what building an agent or
a workflow takes. It runs on the assistant's models, so it's there when the
assistant is; its calls count as the assistant's for the organization.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, status
from forge_common.adk.usage import Attribution, price_from
from forge_common.model_provider import ModelProviderAuthError
from google.adk.models import BaseLlm
from google.genai import errors as genai_errors
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from forge_admin.api.routes.common import NodeId, Session
from forge_admin.assistant.forge import APP_NAME
from forge_admin.assistant.runtime import AgentRuntime
from forge_admin.auth.access import CurrentUser, Enforcer, Level, Scope, authorize
from forge_admin.instruction_writer.brief import Brief, render, resolve
from forge_admin.instruction_writer.writer import WriterError, write
from forge_admin.overview.recording import ASSISTANT

logger = logging.getLogger(__name__)

router = APIRouter(tags=["instruction writer"])

MANAGE = "agents:manage"
#: How long the writer may take.
WRITE_SECONDS = 120.0
NOT_SET_UP = (
    "The instruction writer runs on the assistant's models, which aren't set up here."
)


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What the agent should do, or what to change in ``current``.
    ask: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ]
    #: The instruction to rework: the field's, or the last draft. Empty for a new one.
    current: str = Field(default="", max_length=60_000)
    brief: Brief


class DraftRead(BaseModel):
    instruction: str
    #: For the person: something to attach, a value to declare, a choice to check.
    notes: list[str]
    #: The model that wrote it (``provider/model``).
    model: str


def _model(request: Request) -> BaseLlm:
    """
    The assistant's models, or the model its agent runs on (a test's).

    :raises HTTPException: 503 when the assistant isn't set up.
    """
    runtime: AgentRuntime | None = getattr(request.app.state, "agents", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    if runtime.unavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, runtime.unavailable)
    models = runtime.models.get(APP_NAME)
    if models is not None:
        return models
    runner = runtime.runners.get(APP_NAME)
    model = getattr(runner.agent, "model", None) if runner is not None else None
    if isinstance(model, BaseLlm):
        return model
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)


def _failed(message: str) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, message)


@router.post("/organizations/{organization_id}/instruction-writer")
async def write_instruction(
    organization_id: NodeId,
    body: WriteRequest,
    request: Request,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> DraftRead:
    """
    Write an agent's instruction from what the person wants and the agent as
    it's set up: its tools by the names it calls them, the agents it hands
    off to, and the values it can put in with ``{{ }}``.
    \f
    :raises HTTPException: 403 without agents:manage; 503 when the assistant
        isn't set up; 502 when the model fails or answers nothing; 504 when
        it takes too long.
    """
    await authorize(session, enforcer, user, MANAGE, Scope(Level.ORG, organization_id))
    model = _model(request)
    state = request.app.state
    found = await resolve(
        body.brief,
        organization_id,
        session,
        chat_agents=getattr(state, "chat_agents", None),
        workflows=getattr(state, "organization_agents", None),
    )
    brief = render(body.brief, found)
    # Nothing's written: the session isn't needed while the model writes.
    await session.close()
    runtime: AgentRuntime = state.agents
    try:
        async with asyncio.timeout(WRITE_SECONDS):
            draft = await write(
                model,
                ask=body.ask,
                current=body.current,
                brief=brief,
                user_id=user,
                # It's the assistant's work, done for this organization.
                attribution=Attribution(
                    organization_id, "assistant", ASSISTANT, "Assistant", user_id=user
                ),
                usage=runtime.usage.record if runtime.usage is not None else None,
                price=price_from(model),
            )
    except TimeoutError:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "The writer took too long; try again, or ask for less.",
        ) from None
    except WriterError as error:
        raise _failed(str(error)) from None
    except genai_errors.APIError as error:
        raise _failed(
            f"The model refused the request ({error.code} {error.status}): {error.message}"
        ) from None
    except ModelProviderAuthError as error:
        # Its message holds no credentials.
        raise _failed(
            f"The writer couldn't sign in to the model's provider: {error}"
        ) from None
    except Exception as error:
        if type(error).__module__.startswith("litellm"):
            raise _failed(
                f"The model refused the request: {str(error)[:500]}"
            ) from None
        logger.exception(
            "The instruction writer failed in organization %s", organization_id
        )
        raise _failed("The writer ran into a problem. Try again.") from None
    logger.info(
        "%s had an instruction written in organization %s (%s)",
        user,
        organization_id,
        draft.model,
    )
    return DraftRead(
        instruction=draft.instruction, notes=draft.notes, model=draft.model
    )
