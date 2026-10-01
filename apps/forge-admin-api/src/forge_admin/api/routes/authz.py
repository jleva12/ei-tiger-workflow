"""The shared Casbin model, and any subject's access for other services."""

from typing import Annotated

from fastapi import APIRouter, Path, Query
from fastapi.responses import PlainTextResponse

from forge_admin.api.routes.common import Session
from forge_admin.api.routes.me import Access
from forge_admin.auth.access import (
    SCOPE_PATTERN,
    CurrentUser,
    Enforcer,
    Scope,
    authorize,
)
from forge_admin.auth.authorization import MODEL_PATH, SUBJECT_PATTERN, load_access

router = APIRouter(prefix="/authz", tags=["authz"])


@router.get("/model", response_class=PlainTextResponse)
async def get_model() -> str:
    """
    Return the Casbin model every enforcer uses, as a ``.conf`` file.
    \f
    :return: The model text.
    """
    return MODEL_PATH.read_text()


@router.get("/subjects/{subject_id}/access")
async def get_subject_access(
    subject_id: Annotated[str, Path(pattern=SUBJECT_PATTERN)],
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    scope: Annotated[str, Query(pattern=SCOPE_PATTERN)] = "site",
) -> Access:
    """
    Return any subject's effective roles and permissions in a scope. Requires
    ``members:read`` there.
    \f
    :param subject_id: The user or service ID.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param scope: The scope; the site by default.
    :return: The roles and permission policy lines.
    """
    domain = await authorize(
        session, enforcer, user, "members:read", Scope.parse(scope)
    )
    roles, policies = await load_access(enforcer, subject_id, domain)
    return Access(
        subject=subject_id, scope=scope, domain=domain, roles=roles, policies=policies
    )
