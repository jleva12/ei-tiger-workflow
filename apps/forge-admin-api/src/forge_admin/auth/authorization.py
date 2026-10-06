"""Casbin access control over MySQL, scoped by the organization hierarchy.

The model is ``casbin_model.conf``. Its policy lives in the ``casbin_rule``
table: ``p`` lines grant permissions to roles, ``g`` lines assign roles to
subjects within a domain pattern. ``p`` lines also grant permissions to the
company's own groups, from an external directory (``group:<name>``): someone
whose token names the group holds them on the whole site. The API writes those lines in the same
transaction as the role, permission and hierarchy records they belong to;
the enforcer reads them.
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import casbin
from casbin.util import key_match
from casbin_async_sqlalchemy_adapter import Adapter
from fastapi import Request
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forge_admin.models import CasbinRule, Permission

MODEL_PATH = Path(__file__).with_name("casbin_model.conf")

# Role keys are "<level>:<name>", where the level is where the role can be
# assigned: "site:admin", "org:admin", "org:member".
ROLE_KEY_PATTERN = r"^(?:site|org):[a-z][a-z0-9_]*$"
# Permission keys are "resource:action", e.g. "agents:run"; either part may
# be "*" ("agents:*", "*:*"). The parts are the Casbin resource and action.
PERMISSION_KEY_PATTERN = r"^(?:[a-z][a-z0-9_]*|\*):(?:[a-z][a-z0-9_]*|\*)$"
# A user or service ID, e.g. a UUID or an email.
SUBJECT_PATTERN = r"^[A-Za-z0-9._@:+-]{1,255}$"
# An external group's Casbin subject is its name after this prefix, e.g.
# "group:Engineering", so no user or role can be one.
GROUP_PREFIX = "group:"
# An organization API key's Casbin subject is its ID after this prefix
# (forge_admin.auth.api_keys), so no user can be one.
API_KEY_SUBJECT_PREFIX = "apikey:"
# An external group's name, as the directory spells it: printable ASCII,
# spaces inside (e.g. "CN=Forge Admins,OU=Groups"), up to 200 characters.
GROUP_NAME_PATTERN = r"^[!-~](?:[ -~]{0,198}[!-~])?$"


# What Casbin reads a policy line's tokens apart by (commas, and the
# brackets it nests), and % so encoding is reversible: percent-encoded in a
# group's subject, so "CN=Admins,OU=Groups" stays one token.
_ESCAPED = {c: f"%{ord(c):02X}" for c in '%,[]()"'}
# casbin_rule's subject column.
MAX_SUBJECT = 255


def group_subject(name: str) -> str:
    """
    :return: The Casbin subject of an external group: ``group:`` and its
        name, with the characters Casbin splits lines on percent-encoded.
    """
    return GROUP_PREFIX + "".join(_ESCAPED.get(c, c) for c in name)


def is_reserved_subject(subject: str) -> bool:
    """
    Whether a subject ID is one no user or service may have: a role key, an
    external group's subject, or an API key's. Roles, groups and subjects
    share Casbin's namespace, so a user named like any of them would hold
    its grants.
    """
    return bool(re.fullmatch(ROLE_KEY_PATTERN, subject)) or subject.startswith(
        (GROUP_PREFIX, API_KEY_SUBJECT_PREFIX)
    )


def is_api_key_subject(subject: str | None) -> bool:
    """:return: Whether the subject is an organization API key's."""
    return bool(subject) and subject.startswith(API_KEY_SUBJECT_PREFIX)  # type: ignore[union-attr]


def new_enforcer(adapter: Adapter | None = None) -> casbin.AsyncEnforcer:
    """
    Create an enforcer for the shared model, with keyMatch as g's domain
    matching function, as every enforcer of this model must have.

    :param adapter: Where the policy is stored; None keeps it in memory.
    :return: The enforcer.
    """
    enforcer = casbin.AsyncEnforcer(str(MODEL_PATH), adapter)
    enforcer.add_named_domain_matching_func("g", key_match)
    return enforcer


def create_enforcer(engine: AsyncEngine) -> casbin.AsyncEnforcer:
    """
    Create the application's enforcer over the ``casbin_rule`` table.

    Nothing is read yet; call ``load_policy()`` before evaluating.

    :param engine: The application's database engine.
    :return: The enforcer.
    """
    return new_enforcer(Adapter(engine, db_class=CasbinRule))


def get_enforcer(request: Request) -> casbin.AsyncEnforcer:
    """
    FastAPI dependency: the enforcer the application created at startup.

    :param request: The current request.
    :return: The enforcer.
    """
    enforcer: casbin.AsyncEnforcer = request.app.state.enforcer
    return enforcer


# Grants: p lines ----------------------------------------------------------


async def granted_permissions(
    session: AsyncSession, role_keys: Sequence[str]
) -> dict[str, list[Permission]]:
    """
    List the permissions granted to each of the given roles, in one query.

    :param session: The request's database session.
    :param role_keys: The roles.
    :return: Each role key mapped to its permissions, ordered by resource and
        action; roles without grants map to an empty list.
    """
    grants: dict[str, list[Permission]] = {key: [] for key in role_keys}
    rows = await session.execute(
        select(CasbinRule.v0, Permission)
        .join(
            Permission,
            (CasbinRule.v1 == Permission.resource)
            & (CasbinRule.v2 == Permission.action),
        )
        .where(CasbinRule.ptype == "p", CasbinRule.v0.in_(role_keys))
        .order_by(Permission.resource, Permission.action)
    )
    for role_key, permission in rows:
        if role_key is not None:
            grants[role_key].append(permission)
    return grants


async def role_grants(session: AsyncSession, role_key: str) -> list[tuple[str, str]]:
    """
    List what a role grants, as its p lines say: each ``(resource, action)``,
    wildcards included, whether or not the permission is a known one.

    :param session: The request's database session.
    :param role_key: The role.
    :return: Its grants, sorted.
    """
    rows = await session.execute(
        select(CasbinRule.v1, CasbinRule.v2).where(
            CasbinRule.ptype == "p", CasbinRule.v0 == role_key
        )
    )
    return sorted((resource or "", action or "") for resource, action in rows)


async def set_role_permissions(
    session: AsyncSession, role_key: str, permissions: Iterable[Permission]
) -> None:
    """
    Replace a role's grants with exactly these permissions.

    Grants the role keeps are left untouched, so their audit columns still
    show when and by whom they were first given.

    :param session: The request's database session.
    :param role_key: The role.
    :param permissions: The permissions the role should have.
    """
    wanted = {(p.resource, p.action) for p in permissions}
    current = await session.scalars(
        select(CasbinRule).where(CasbinRule.ptype == "p", CasbinRule.v0 == role_key)
    )
    for rule in current:
        # p lines always carry a resource and an action.
        grant = (rule.v1 or "", rule.v2 or "")
        if grant in wanted:
            wanted.discard(grant)
        else:
            await session.delete(rule)
    session.add_all(
        CasbinRule(ptype="p", v0=role_key, v1=resource, v2=action)
        for resource, action in sorted(wanted)
    )


async def delete_role_rules(session: AsyncSession, role_key: str) -> None:
    """
    Remove a role's grants and every assignment of it, in every scope.

    :param session: The request's database session.
    :param role_key: The role being deleted.
    """
    await session.execute(
        delete(CasbinRule).where(
            ((CasbinRule.ptype == "p") & (CasbinRule.v0 == role_key))
            | ((CasbinRule.ptype == "g") & (CasbinRule.v1 == role_key))
        )
    )


async def move_permission_rules(
    session: AsyncSession, old: tuple[str, str], new: tuple[str, str]
) -> None:
    """
    Point every grant of a permission at its new resource and action.

    :param session: The request's database session.
    :param old: The permission's previous ``(resource, action)``.
    :param new: Its new ``(resource, action)``.
    """
    await session.execute(
        update(CasbinRule)
        .where(
            CasbinRule.ptype == "p", CasbinRule.v1 == old[0], CasbinRule.v2 == old[1]
        )
        .values(v1=new[0], v2=new[1])
    )


async def delete_permission_rules(
    session: AsyncSession, resource: str, action: str
) -> None:
    """
    Revoke a permission from every role.

    :param session: The request's database session.
    :param resource: The permission's resource.
    :param action: The permission's action.
    """
    await session.execute(
        delete(CasbinRule).where(
            CasbinRule.ptype == "p", CasbinRule.v1 == resource, CasbinRule.v2 == action
        )
    )


# Assignments: g lines -------------------------------------------------------


async def assignments_around(
    session: AsyncSession,
    domain: str,
    *,
    above: bool,
    below: bool,
    api_keys: bool = False,
) -> list[CasbinRule]:
    """
    List the role assignments made in a scope and, optionally, those that
    apply to it from above (the site's, in an organization) and those made
    inside it (every organization's, on the site).

    :param session: The request's database session.
    :param domain: The scope's domain, ``org:<id>`` or ``site``.
    :param above: In an organization, include the site's assignments.
    :param below: On the site, include every organization's assignments.
    :param api_keys: Include API keys' roles; they're no one's membership,
        so by default they're left out.
    :return: The g lines, ordered by subject, role and domain pattern.
    """
    site = domain == "site"
    # An assignment's pattern: "*" on the site, "org:<id>*" in an organization.
    patterns = ["*" if site else f"{domain}*"]
    if above and not site:
        patterns.append("*")
    matches = [CasbinRule.v2.in_(patterns)]
    if below and site:
        matches.append(CasbinRule.v2 != "*")
    query = select(CasbinRule).where(CasbinRule.ptype == "g", or_(*matches))
    if not api_keys:
        query = query.where(CasbinRule.v0.not_like(f"{API_KEY_SUBJECT_PREFIX}%"))
    result = await session.scalars(
        query.order_by(CasbinRule.v0, CasbinRule.v1, CasbinRule.v2)
    )
    return list(result)


async def roles_of_subjects(
    session: AsyncSession, subjects: Sequence[str], pattern: str
) -> dict[str, str]:
    """
    Look up the role each subject holds in one scope, in one query.

    :param session: The request's database session.
    :param subjects: The subjects, e.g. API keys'.
    :param pattern: The scope's domain pattern, e.g. ``org:<id>*``.
    :return: Each subject that holds a role there mapped to it (the first,
        should one hold several).
    """
    if not subjects:
        return {}
    rows = await session.execute(
        select(CasbinRule.v0, CasbinRule.v1)
        .where(
            CasbinRule.ptype == "g",
            CasbinRule.v0.in_(subjects),
            CasbinRule.v2 == pattern,
        )
        .order_by(CasbinRule.v0, CasbinRule.v1)
    )
    roles: dict[str, str] = {}
    for subject, role in rows:
        if subject is not None and role is not None:
            roles.setdefault(subject, role)
    return roles


async def assignment_counts(
    session: AsyncSession, role_keys: Sequence[str]
) -> dict[str, int]:
    """
    Count the assignments of each of the given roles, across every scope.

    :param session: The request's database session.
    :param role_keys: The roles.
    :return: Each role key mapped to how many times it is assigned.
    """
    counts = dict.fromkeys(role_keys, 0)
    rows = await session.execute(
        select(CasbinRule.v1, func.count())
        .where(CasbinRule.ptype == "g", CasbinRule.v1.in_(role_keys))
        .group_by(CasbinRule.v1)
    )
    for role_key, count in rows:
        if role_key is not None:
            counts[role_key] = count
    return counts


async def assignments_of(session: AsyncSession, subject: str) -> list[CasbinRule]:
    """
    List every role a subject holds, in every scope.

    :param session: The request's database session.
    :param subject: The user or service ID.
    :return: The subject's g lines, ordered by domain pattern and role.
    """
    result = await session.scalars(
        select(CasbinRule)
        .where(CasbinRule.ptype == "g", CasbinRule.v0 == subject)
        .order_by(CasbinRule.v2, CasbinRule.v1)
    )
    return list(result)


async def find_assignment(
    session: AsyncSession, subject: str, role_key: str, pattern: str
) -> CasbinRule | None:
    """
    Find one role assignment.

    :param session: The request's database session.
    :param subject: The user or service ID.
    :param role_key: The role.
    :param pattern: The scope's domain pattern.
    :return: The g line, or None.
    """
    result: CasbinRule | None = await session.scalar(
        select(CasbinRule).where(
            CasbinRule.ptype == "g",
            CasbinRule.v0 == subject,
            CasbinRule.v1 == role_key,
            CasbinRule.v2 == pattern,
        )
    )
    return result


async def remove_assignments_in(session: AsyncSession, pattern: str) -> None:
    """
    Revoke every role assigned in a scope that is being deleted.

    :param session: The request's database session.
    :param pattern: The scope's domain pattern.
    """
    await session.execute(
        delete(CasbinRule).where(CasbinRule.ptype == "g", CasbinRule.v2 == pattern)
    )


async def load_access(
    enforcer: casbin.AsyncEnforcer,
    subject: str,
    domain: str,
    groups: Iterable[str] = (),
) -> tuple[list[str], list[list[str]]]:
    """
    Evaluate a subject's effective roles and permissions in a domain.

    The policy is reloaded from MySQL first, so every instance answers from
    the current rules.

    :param enforcer: The Casbin enforcer.
    :param subject: The user or service ID.
    :param domain: The scope's domain, e.g. ``org:<id>``.
    :param groups: The external groups the subject is in (their token's),
        whose linked permissions they hold everywhere.
    :return: The roles the subject holds there (including those held above
        it), and the p lines granting their permissions, ``[role, resource,
        action]``, and their groups', ``[group:<name>, resource, action]``.
    """
    await enforcer.load_policy()
    roles = sorted(await enforcer.get_implicit_roles_for_user(subject, domain))
    held = set(roles) | {group_subject(name) for name in groups}
    # Grants carry no domain, so read them directly rather than through
    # get_implicit_permissions_for_user, which filters grants by domain.
    policies = sorted(rule for rule in enforcer.get_policy() if rule[0] in held)
    return roles, policies
