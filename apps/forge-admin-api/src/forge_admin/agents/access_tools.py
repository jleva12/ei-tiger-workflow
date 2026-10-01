"""The access tools: what an assistant needs to tell a person what they may
do in Forge and why something was refused, who holds which role where, and,
for people who may, to give a member a role or take one away.

Every tool calls the admin API's own routes (``/me``, ``/roles``,
``/scopes/{scope}/members``, ``/authz/subjects/{id}/access``, ``/users``) as
the conversation's user (``person_api.py``), whom the ``/agents`` routes
checked is the signed-in person: anyone reads their own profile, roles and
permissions and the roles there are; reading who holds roles in a scope, or
someone else's access, takes ``members:read`` there, and giving or taking
away a role takes ``members:update``, exactly as in the web console, and each
change is audited as theirs. Who they are comes from the conversation, never
from the model's arguments: a member's ID is only ever the one whose role or
access a call is about.

Roles and permissions themselves (creating, editing, deleting them) and
people's records are the administration tools', not these.

Tools that change something (give a role, take one away) ask the person to
confirm each call first, unless the toolset is made with
``confirm_changes=False``. As a :class:`ForgeBaseToolset`, no tool raises: a
refused call answers ``failed`` with the API's reason and how to go on, and
large results are cut down.
"""

import logging
import re
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI
from forge_common.adk import ToolFailure
from google.adk.tools.tool_context import ToolContext

from forge_admin.agents.person_api import PersonApi
from forge_admin.agents.route_tools import ApiRefused, RouteToolset, pick
from forge_admin.auth.access import SCOPE_PATTERN
from forge_admin.auth.authorization import ROLE_KEY_PATTERN

logger = logging.getLogger(__name__)

Level = Literal["site", "org"]

# The routes' own formats, matched whole (``fullmatch``: ``$`` alone would let
# a trailing newline through), so no argument can reach another route
# through the path. A member's ID is the routes' subject ID (a UUID, an email,
# a service's name) that starts with a letter or digit, so it's never "." or
# "..".
SCOPE = re.compile(SCOPE_PATTERN)
ROLE_KEY = re.compile(ROLE_KEY_PATTERN)
MEMBER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+-]{0,254}")
# A permission to check: one resource and one action, e.g. workflows:run.
PERMISSION = re.compile(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*")

# The tools, by name: those that read, then those that change something.
READ_TOOLS = (
    "get_my_profile",
    "get_my_access",
    "list_my_memberships",
    "list_my_organizations",
    "list_available_roles",
    "get_available_role",
    "find_people",
    "list_scope_members",
    "get_member_access",
)
CHANGE_TOOLS = (
    "assign_member_role",
    "remove_member_role",
)
TOOL_NAMES = frozenset(READ_TOOLS + CHANGE_TOOLS)

# Added to the agent's instruction while it has these tools.
INSTRUCTION = """
Access and people:
- The access tools read the person's own profile, roles and permissions, the \
roles there are and what each grants, and who holds which role in a scope; \
and, where the person may (members:update), give a member a role or take one \
away. They act as the person: what they could see and do in the web console, \
and nothing more.
- A scope is written site or org:<organization ID>. A role's key starts \
with the level it's given at (e.g. org:member); it's given only in a scope of \
that level, and a site role applies in every organization too. Take IDs from \
the page, the person's organizations or memberships, or earlier results; \
never invent them.
- When something was refused, or the person asks whether they can do \
something, explain it: the refusal names the missing permission (e.g. \
"Requires workflows:run"). Check it with get_my_access in that scope and \
permission; find the roles that grant it with list_available_roles (grants); \
and say who can give one: people holding a role that grants members:update \
in that scope or above it (list_scope_members with include_above, if the \
person may read members; otherwise their organization's administrators). \
Never guess what a role allows from its name: read what it grants.
- Before you give or take away a role, agree who, which role and where: find \
the member with find_people or list_scope_members, and the role with \
list_available_roles. Each change asks the person to confirm it first. Taking \
a role away in one scope leaves the same role held above it.
- When a call fails, say why in plain words and follow its suggested fixes; \
don't retry one the person lacks the permission for.
"""

# The most people find_people answers with.
MAX_PEOPLE = 25


class AccessToolset(RouteToolset):
    """
    Tools to read the person's own access, the roles there are, and who
    holds which role in a scope, and to give or take away a member's role,
    as the person the assistant is talking to.

    :param api: The admin API, called as the person.
    :param confirm_changes: Ask the person to confirm each change first.
    :param kwargs: :class:`ForgeBaseToolset`'s (``max_result_chars``,
        ``tool_filter``, ``tool_name_prefix``, ...).
    """

    READ_TOOLS = READ_TOOLS
    CHANGE_TOOLS = CHANGE_TOOLS
    STATUS_FIXES = {
        403: [
            "The person lacks the permission the reason names, in that scope. Find "
            "the roles that grant it with list_available_roles (grants=that "
            "permission), and who can give one: holders of a role granting "
            "members:update there or above (list_scope_members with include_above, "
            "if the person may read members). Tell them; don't retry."
        ],
        404: [
            "Check the scope and IDs: an organization's scope is org:<its ID> "
            "(list_my_organizations or list_my_memberships, or the page); a role's key from "
            "list_available_roles; a member's ID from find_people or "
            "list_scope_members."
        ],
        422: [
            "Fix the arguments as the reason says. A role is given only in a scope of "
            "its own level (an org:... role in an org:<ID> scope; list_available_roles "
            "with level lists them), and must be one of list_available_roles'."
        ],
    }

    # -- The person's own access ---------------------------------------------

    async def get_my_profile(self, tool_context: ToolContext) -> dict[str, Any]:
        """
        Who the person is in Forge: their user ID, name, email and network
        ID (msid). Use it when they ask who they're signed in as, or for
        their own ID.
        """
        me = await self._call(tool_context, "GET", "/me") or {}
        return {
            "subject": me.get("subject"),
            "user": pick(me.get("user"), PROFILE_FIELDS) or None,
        }

    async def get_my_access(
        self,
        scope: str,
        tool_context: ToolContext,
        permission: str | None = None,
    ) -> dict[str, Any]:
        """
        The person's roles in a scope (held there or above it) and every
        permission those roles grant them there, each with the roles that
        grant it. Give a permission to check whether they have it and which
        of their roles grants it: use this to explain a refusal ("Requires
        workflows:run") or to answer "can I ... here?".

        Args:
            scope: Where: site or org:<organization ID>. An organization's
                permissions are checked in its scope, so use org:<ID> for
                anything done in an organization.
            permission: A permission to check, resource:action, e.g.
                workflows:run or members:update.
        """
        wanted = _permission(permission) if permission else None
        answer = await self._call(
            tool_context, "GET", "/me/access", params={"scope": _scope(scope)}
        )
        return _access(answer or {}, wanted)

    async def list_my_memberships(self, tool_context: ToolContext) -> list[Any]:
        """
        Every role the person holds, and the scope they were given it in
        (site or org:<ID>). A site role applies in every organization too. For
        their organizations' names, use list_my_organizations.
        """
        return await self._call(tool_context, "GET", "/me/memberships") or []

    async def list_my_organizations(self, tool_context: ToolContext) -> list[Any]:
        """
        The organizations the person is a member of (holds a role in), with
        the person's roles in each. A site role doesn't make them a member of
        one. An organization's scope is org:<its id>.
        """
        return await self._call(tool_context, "GET", "/me/organizations") or []

    # -- Roles ---------------------------------------------------------------

    async def list_available_roles(
        self,
        tool_context: ToolContext,
        level: Level | None = None,
        grants: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        The roles there are, each with its key (what assign_member_role
        takes), the level it's given at, and the permissions it grants.
        Filter by grants to find which roles would give someone a permission
        they were refused.

        Args:
            level: Only roles given at this level: site or org.
                A scope takes only roles of its own level.
            grants: Only roles that grant this permission, resource:action,
                e.g. workflows:run (a role granting workflows:* or *:* grants it
                too).
        """
        wanted = _permission(grants) if grants else None
        roles = await self._call(tool_context, "GET", "/roles") or []
        return [
            _role(role)
            for role in roles
            if isinstance(role, dict)
            and (level is None or role.get("level") == level)
            and (wanted is None or _role_grants(role, wanted))
        ]

    async def get_available_role(
        self, role: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        One role: its name, description, the level it's given at, how many
        times it's given, and each permission it grants, described.

        Args:
            role: The role's key, e.g. org:admin, from list_available_roles.
        """
        record = await self._call(tool_context, "GET", f"/roles/{_role_key(role)}")
        return _role(record or {}, described=True)

    # -- Members -------------------------------------------------------------

    async def find_people(
        self, search: str, tool_context: ToolContext, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        People in Forge whose name, email, network ID (msid) or user ID
        contains the search, with their user ID: use it to find the member
        whose role to give, take away or read.

        Args:
            search: Part of a name, email, msid or user ID.
            limit: How many at most, 1 to 25.
        """
        needle = (search or "").strip().casefold()
        if not needle:
            raise ToolFailure("search must name part of a person's name or email")
        people = await self._call(tool_context, "GET", "/users") or []
        found = [
            _person(person)
            for person in people
            if isinstance(person, dict) and needle in _haystack(person)
        ]
        return found[: max(1, min(limit, MAX_PEOPLE))]

    async def list_scope_members(
        self,
        scope: str,
        tool_context: ToolContext,
        include_above: bool = False,
        include_below: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Who holds which role in a scope, by person (with their name and
        email where they're a Forge user), each role with the scope it was
        given in and who gave it when. Needs members:read in the scope.

        Args:
            scope: Where: site or org:<organization ID>.
            include_above: Also the roles given in the scopes above that
                apply here, e.g. the site's administrators in an
                organization: everyone with a say in the scope.
            include_below: Also the roles given in the scopes inside it,
                e.g. the site's organizations' members.
        """
        members = await self._call(
            tool_context,
            "GET",
            f"/scopes/{_scope(scope)}/members",
            params={"above": include_above or None, "below": include_below or None},
        )
        if not members:
            return []
        return _members(members, await self._people(tool_context))

    async def get_member_access(
        self,
        member_id: str,
        scope: str,
        tool_context: ToolContext,
        permission: str | None = None,
    ) -> dict[str, Any]:
        """
        Someone else's roles in a scope (held there or above it) and the
        permissions those grant them there, as get_my_access answers for the
        person. Give a permission to check whether they have it. Needs
        members:read in the scope.

        Args:
            member_id: The member's user ID, from find_people or
                list_scope_members.
            scope: Where: site or org:<organization ID>.
            permission: A permission to check, resource:action, e.g.
                workflows:run.
        """
        wanted = _permission(permission) if permission else None
        answer = await self._call(
            tool_context,
            "GET",
            f"/authz/subjects/{_member_id(member_id)}/access",
            params={"scope": _scope(scope)},
        )
        return _access(answer or {}, wanted)

    # -- Changing roles ------------------------------------------------------

    async def assign_member_role(
        self, scope: str, member_id: str, role: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Give a member a role in a scope, as the person, who needs
        members:update there. The role applies in the scope and every scope
        inside it; giving one they already hold there changes nothing. Agree
        who, which role and where first.

        Args:
            scope: Where: site or org:<organization ID>.
            member_id: Whose role: their user ID, from find_people or
                list_scope_members.
            role: The role's key, from list_available_roles; its level must
                be the scope's (an org:... role in an org:<ID> scope).
        """
        path = _assignment_path(scope, member_id, role)
        assigned = await self._call(tool_context, "PUT", path)
        return {"assigned": _assignment(assigned or {})}

    async def remove_member_role(
        self, scope: str, member_id: str, role: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Take a role away from a member in the scope it was given in, as the
        person, who needs members:update there. The same role given above
        the scope still applies; list_scope_members shows where each was
        given. Taking away one they don't hold there changes nothing.

        Args:
            scope: The scope the role was given in: site or
                org:<organization ID>.
            member_id: Whose role: their user ID, from list_scope_members.
            role: The role's key, e.g. org:member.
        """
        path = _assignment_path(scope, member_id, role)
        await self._call(tool_context, "DELETE", path)
        return {
            "removed": {"subjectId": member_id, "role": role, "scope": scope},
        }

    async def _people(self, tool_context: ToolContext) -> dict[str, dict[str, Any]]:
        """Forge's users by ID, to name members; none when they can't be read."""
        try:
            people = await self._call(tool_context, "GET", "/users") or []
        except ApiRefused as refusal:
            logger.info("Can't name the members: %s", refusal.reason)
            return {}
        return {
            str(person["id"]): _person(person)
            for person in people
            if isinstance(person, dict) and person.get("id")
        }


def toolset(app: FastAPI, **kwargs: Any) -> AccessToolset | None:
    """
    The access tools, calling ``app``'s routes as each conversation's person.

    :param app: The admin API's application.
    :param kwargs: :class:`AccessToolset`'s options.
    :return: The toolset, or None without ``jwt_secret``, which the person's
        tokens are signed with.
    """
    if app.state.settings.jwt_secret is None:
        logger.warning(
            "FORGE_ADMIN_JWT_SECRET isn't set: the assistant can't read or change "
            "access as the people it talks to"
        )
        return None
    return AccessToolset(PersonApi.of_app(app), **kwargs)


# -- Checking arguments ------------------------------------------------------


def _scope(value: str) -> str:
    """A scope, as the routes write it; only letters, digits, ":" and "-"."""
    if not SCOPE.fullmatch(value or ""):
        raise ToolFailure(
            f"scope must be site or org:<ID> (the ID in lowercase UUID form), "
            f"not {value!r}"
        )
    return value


def _member_id(value: str) -> str:
    if not MEMBER_ID.fullmatch(value or ""):
        raise ToolFailure(
            f"member_id must be a member's user ID, from find_people or "
            f"list_scope_members, not {value!r}"
        )
    return quote(value, safe="")


def _role_key(value: str) -> str:
    if not ROLE_KEY.fullmatch(value or ""):
        raise ToolFailure(
            f"role must be a role's key, <level>:<name> such as org:member, from "
            f"list_available_roles, not {value!r}"
        )
    return quote(value, safe="")


def _permission(value: str) -> tuple[str, str]:
    """A permission to check, as its resource and action."""
    if not PERMISSION.fullmatch(value or ""):
        raise ToolFailure(
            f"A permission is resource:action, e.g. workflows:run, not {value!r}"
        )
    resource, _, action = value.partition(":")
    return resource, action


def _assignment_path(scope: str, member_id: str, role: str) -> str:
    """Where a member's role in a scope is given or taken away."""
    scope = _scope(scope)
    level = "site" if scope == "site" else scope.partition(":")[0]
    key = _role_key(role)
    if role.partition(":")[0] != level:
        raise ToolFailure(
            f"{role} is given in {role.partition(':')[0]} scopes, not in {scope}",
            [
                f"Pick a {level} role with list_available_roles(level={level!r}), or "
                f"give this one in a {role.partition(':')[0]} scope."
            ],
        )
    return f"/scopes/{scope}/members/{_member_id(member_id)}/roles/{key}"


# -- Summaries ---------------------------------------------------------------

PROFILE_FIELDS = ("id", "first_name", "last_name", "email", "msid")


def _covers(resource: str, action: str, wanted: tuple[str, str]) -> bool:
    """Whether a grant of resource:action covers a permission, as Casbin
    matches them (casbin_model.conf): either part may be "*"."""
    return resource in ("*", wanted[0]) and action in ("*", wanted[1])


def _access(answer: dict[str, Any], wanted: tuple[str, str] | None) -> dict[str, Any]:
    """
    Someone's access in a scope: their roles, and each permission with the
    roles granting it, from the route's Casbin lines ``[role, resource,
    action]``; with ``wanted``, whether they hold it.
    """
    grants: dict[str, set[str]] = {}
    lines = [
        [str(part) for part in line[:3]]
        for line in answer.get("policies") or []
        if isinstance(line, list) and len(line) >= 3
    ]
    for role, resource, action in lines:
        grants.setdefault(f"{resource}:{action}", set()).add(role)
    summary: dict[str, Any] = {
        **pick(answer, ("subject", "scope", "domain")),
        "roles": answer.get("roles") or [],
        "permissions": [
            {"permission": key, "grantedBy": sorted(roles)}
            for key, roles in sorted(grants.items())
        ],
    }
    if wanted is not None:
        granted_by = sorted(
            {
                role
                for role, resource, action in lines
                if _covers(resource, action, wanted)
            }
        )
        summary["check"] = {
            "permission": ":".join(wanted),
            "allowed": bool(granted_by),
            "grantedBy": granted_by,
        }
    return summary


def _role_grants(role: dict[str, Any], wanted: tuple[str, str]) -> bool:
    return any(
        _covers(str(p.get("resource")), str(p.get("action")), wanted)
        for p in role.get("permissions") or []
        if isinstance(p, dict)
    )


def _role(record: dict[str, Any], *, described: bool = False) -> dict[str, Any]:
    """A role and what it grants; each grant described on its own page."""
    permissions = [p for p in record.get("permissions") or [] if isinstance(p, dict)]
    return {
        **pick(record, ("key", "level", "name", "description")),
        "permissions": [
            pick(p, ("key", "description")) if described else p.get("key")
            for p in permissions
        ],
        "memberCount": record.get("member_count"),
    }


def _person(record: dict[str, Any]) -> dict[str, Any]:
    name = " ".join(
        str(record[part]) for part in ("first_name", "last_name") if record.get(part)
    )
    return {
        "id": record.get("id"),
        **({"name": name} if name else {}),
        **pick(record, ("email", "msid")),
    }


def _haystack(record: dict[str, Any]) -> str:
    parts = (
        f"{record.get('first_name') or ''} {record.get('last_name') or ''}",
        record.get("email"),
        record.get("msid"),
        record.get("id"),
    )
    return "\n".join(str(part) for part in parts if part).casefold()


def _assignment(record: dict[str, Any]) -> dict[str, Any]:
    """One role given in one scope, and who gave it when."""
    summary = {
        "subjectId": record.get("subject_id"),
        "role": record.get("role"),
        "scope": record.get("scope"),
        "assignedBy": record.get("created_by"),
        "assignedAt": record.get("created_at"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _members(
    assignments: Iterable[Any], people: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Role assignments grouped by member, in the route's order."""
    members: dict[str, dict[str, Any]] = {}
    for record in assignments:
        if not isinstance(record, dict):
            continue
        subject = str(record.get("subject_id"))
        member = members.get(subject)
        if member is None:
            person = people.get(subject, {})
            member = members[subject] = {
                "subjectId": subject,
                **{key: value for key, value in person.items() if key != "id"},
                "roles": [],
            }
        member["roles"].append(
            {
                key: value
                for key, value in _assignment(record).items()
                if key != "subjectId"
            }
        )
    return list(members.values())
