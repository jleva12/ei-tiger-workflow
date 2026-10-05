"""Who holds which role in a scope: the site or a place in the hierarchy."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from forge_admin.api.routes.common import Audited, Session, as_read
from forge_admin.auth.access import (
    SCOPE_PATTERN,
    CurrentUser,
    Enforcer,
    Scope,
    assignment_pattern,
    authorize,
)
from forge_admin.auth.authorization import (
    ROLE_KEY_PATTERN,
    SUBJECT_PATTERN,
    assignments_around,
    find_assignment,
    is_reserved_subject,
)
from forge_admin.models import CasbinRule, Role

router = APIRouter(prefix="/scopes/{scope}/members", tags=["members"])

ScopePath = Annotated[
    str,
    Path(
        pattern=SCOPE_PATTERN,
        description="site or org:<id>",
    ),
]
SubjectPath = Annotated[
    str, Path(pattern=SUBJECT_PATTERN, description="User or service ID")
]
RolePath = Annotated[str, Path(pattern=ROLE_KEY_PATTERN, examples=["org:member"])]


class MemberRead(Audited):
    subject_id: str
    role: str
    scope: str


def _read(rule: CasbinRule) -> MemberRead:
    return as_read(
        MemberRead,
        rule,
        subject_id=rule.v0,
        role=rule.v1,
        scope=str(Scope.from_pattern(rule.v2 or "")),
    )


@router.get("")
async def list_members(
    scope: ScopePath,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
    above: Annotated[
        bool, Query(description="Also the roles assigned above that apply here")
    ] = False,
    below: Annotated[
        bool, Query(description="Also the roles assigned in the scopes inside it")
    ] = False,
) -> list[MemberRead]:
    """
    List the roles assigned in this scope. With ``above`` and ``below``, also
    those that apply to it from the scopes above, and those assigned inside
    it: everything active there. Each assignment names the scope it was made in.
    \f
    :param scope: The scope.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :param above: Include the roles assigned in the site and the ancestors.
    :param below: Include the roles assigned in the scopes inside it.
    :return: Assignments ordered by subject, role and scope.
    """
    domain = await authorize(
        session, enforcer, user, "members:read", Scope.parse(scope)
    )
    rules = await assignments_around(session, domain, above=above, below=below)
    return [_read(rule) for rule in rules]


@router.put("/{subject_id}/roles/{role}")
async def assign_role(
    scope: ScopePath,
    subject_id: SubjectPath,
    role: RolePath,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> MemberRead:
    """
    Give a subject a role in this scope; it also applies below the scope.
    Assigning a role the subject already holds changes nothing.
    \f
    :param scope: The scope.
    :param subject_id: The user or service ID.
    :param role: The role key; its level must match the scope's.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    :return: The assignment.
    """
    target = Scope.parse(scope)
    record = await session.get(Role, role)
    if record is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown role {role}"
        )
    if record.level != target.level:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{role} is assigned at {record.level} scopes, not {target.level}",
        )
    # Roles, groups and subjects share Casbin's namespace: a subject named
    # like a role would make that role inherit the assigned one, and one
    # named like a group would hold its linked permissions.
    if is_reserved_subject(subject_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Subject IDs cannot look like role keys or start with group:",
        )
    domain = await authorize(session, enforcer, user, "members:update", target)
    pattern = assignment_pattern(domain)
    rule = await find_assignment(session, subject_id, role, pattern)
    if rule is None:
        rule = CasbinRule(ptype="g", v0=subject_id, v1=role, v2=pattern)
        session.add(rule)
        await session.commit()
    return _read(rule)


@router.delete("/{subject_id}/roles/{role}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_role(
    scope: ScopePath,
    subject_id: SubjectPath,
    role: RolePath,
    user: CurrentUser,
    session: Session,
    enforcer: Enforcer,
) -> None:
    """
    Take a role away from a subject in this scope.
    \f
    :param scope: The scope.
    :param subject_id: The user or service ID.
    :param role: The role key.
    :param user: The caller.
    :param session: The request's database session.
    :param enforcer: The Casbin enforcer.
    """
    domain = await authorize(
        session, enforcer, user, "members:update", Scope.parse(scope)
    )
    rule = await find_assignment(session, subject_id, role, assignment_pattern(domain))
    if rule is not None:
        await session.delete(rule)
        await session.commit()
