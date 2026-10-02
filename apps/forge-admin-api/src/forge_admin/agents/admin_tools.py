"""The administration tools: what an assistant needs to read and change
Forge's hierarchy and its access model, as the person it's talking to.

Every tool calls the admin API's own routes as the conversation's user
(``person_api.py``), whom the ``/agents`` routes checked is the signed-in
person: a tool reads only the organizations they may read, and changes only
what they may change (``organizations:*`` in the scope, ``roles:*``,
``permissions:*`` and ``users:*`` on the site), exactly as on the admin pages, and each change
is audited as theirs. Who they are comes from the conversation, never from
the model's arguments: a user ID here is only ever the user to read or
change.

Tools that change something (create, update) ask the person to confirm each
call first, unless the toolset is made with ``confirm_changes=False``.
Nothing here deletes: removing an organization, role, permission or user
takes too much with it to leave to the assistant, so the person does it on
the admin pages. Who holds which role where is the access
tools' (``members.py``), not these. As a :class:`ForgeBaseToolset`, no tool
raises: a refused call answers ``failed`` with the API's reason and how to
go on, and large results are cut down.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI
from forge_common.adk import ToolFailure
from google.adk.tools.tool_context import ToolContext

from forge_admin.agents.person_api import PersonApi
from forge_admin.agents.route_tools import RouteToolset, given, pick
from forge_admin.auth.access import UUID_PATTERN
from forge_admin.auth.authorization import ROLE_KEY_PATTERN, SUBJECT_PATTERN

logger = logging.getLogger(__name__)

# The routes' own ID formats, matched whole (a trailing newline too), so no
# argument can reach another route through the path. A subject ID may hold
# dots, but never only dots: "/users/.." is the users' parent.
NODE_ID = re.compile(UUID_PATTERN, re.IGNORECASE)
ROLE_KEY = re.compile(ROLE_KEY_PATTERN)
SUBJECT = re.compile(SUBJECT_PATTERN)
ONLY_DOTS = re.compile(r"\.+")
# A permission's ID is a MySQL INT.
MAX_PERMISSION_ID = 2**31 - 1

# The tools, by name: those that read, then those that change something.
READ_TOOLS = (
    "list_organizations",
    "get_organization",
    "list_role_definitions",
    "get_role_definition",
    "list_permission_definitions",
    "get_permission_definition",
    "list_users",
    "get_user",
)
CHANGE_TOOLS = (
    "create_organization",
    "update_organization",
    "create_role_definition",
    "update_role_definition",
    "create_permission_definition",
    "update_permission_definition",
    "create_user",
    "update_user",
)
TOOL_NAMES = frozenset(READ_TOOLS + CHANGE_TOOLS)

# Added to the agent's instruction while it has these tools.
INSTRUCTION = """
Administration:
- The administration tools read and change Forge's organizations and access \
model as the person: the organizations; the role definitions (what each role \
grants) and the permission definitions; and the users roles can be given to. They do what \
the person could on the admin pages, and nothing more.
- These are administrative changes, and they reach everyone below the \
scope: a role's permissions change for everyone who holds it, everywhere; \
a permission's new key applies in every role that grants it. Before any \
change, confirm with the person exactly what and where: the names, the \
scope (the site or which organization), and the role, permission or user. \
Each change also asks them to confirm it before it happens.
- Take IDs and keys from the page or from earlier results; never invent \
them. Read a record before you change it.
- Deleting isn't available to you: to delete an organization, role, \
permission or user, the person does it on the admin pages. \
These tools don't give people roles in a scope either.
- When a call fails, say why in plain words and follow its suggested fixes; \
don't retry one the person lacks the permission for.
"""

# The most users one list shows.
MAX_USERS = 200


class AdministrationToolset(RouteToolset):
    """
    Tools to find, read, create and update organizations, role definitions,
    permission definitions and users, as the person the assistant is talking
    to. None deletes.

    :param api: The admin API, called as the person.
    :param confirm_changes: Ask the person to confirm each change first.
    :param kwargs: :class:`ForgeBaseToolset`'s (``max_result_chars``,
        ``tool_filter``, ``tool_name_prefix``, ...).
    """

    READ_TOOLS = READ_TOOLS
    CHANGE_TOOLS = CHANGE_TOOLS
    STATUS_FIXES = {
        403: [
            "The person lacks the permission this needs there (the reason names "
            "it). Organizations, roles, permissions and users are created or "
            "changed with permissions on the whole site, which site administrators "
            "hold. Tell them, and who can do it; don't retry."
        ],
        404: [
            "Check the IDs: list_organizations for organizations, "
            "list_role_definitions for role keys, list_permission_definitions for "
            "permission IDs, list_users for user IDs."
        ],
        409: [
            "The name, key, email or MS ID is taken, as the reason says: ask the "
            "person for another, or find the existing record with the list tools "
            "and use that."
        ],
        422: [
            "Fix the arguments as the reason says, then call again; list_users has "
            "the user IDs and list_permission_definitions the permission IDs."
        ],
    }

    # -- The hierarchy -------------------------------------------------------

    async def list_organizations(self, tool_context: ToolContext) -> list[Any]:
        """
        The organizations the person can see, by name. Organizations are the
        one level of Forge's hierarchy below the site, where the work happens.
        Start here for an organization's ID.
        """
        organizations = await self._call(tool_context, "GET", "/organizations")
        return [pick(org, ORGANIZATION_FIELDS) for org in organizations or []]

    async def get_organization(
        self, organization_id: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        One organization: its name, description, and who last changed it and
        when.

        Args:
            organization_id: The organization's ID, from the page or
                list_organizations.
        """
        organization = await self._call(
            tool_context,
            "GET",
            f"/organizations/{_node(organization_id, 'organization_id')}",
        )
        return pick(organization, ORGANIZATION_FIELDS + AUDIT_FIELDS)

    async def create_organization(
        self, name: str, tool_context: ToolContext, description: str | None = None
    ) -> dict[str, Any]:
        """
        Create an organization, a new top of the hierarchy. Only site
        administrators can. Agree its name with the person first; no two
        organizations share one.

        Args:
            name: Its name, 1 to 200 characters.
            description: What it is, in a sentence or two.
        """
        organization = await self._call(
            tool_context,
            "POST",
            "/organizations",
            json=given(name=name, description=description),
        )
        return pick(organization, ORGANIZATION_FIELDS + AUDIT_FIELDS)

    async def update_organization(
        self,
        organization_id: str,
        tool_context: ToolContext,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Rename an organization or change its description. What's left out
        stays as it is.

        Args:
            organization_id: The organization's ID.
            name: Its new name, 1 to 200 characters.
            description: Its new description; an empty one clears it.
        """
        path = f"/organizations/{_node(organization_id, 'organization_id')}"
        body = _changes(name=name, description=description)
        organization = await self._call(tool_context, "PATCH", path, json=body)
        return pick(organization, ORGANIZATION_FIELDS + AUDIT_FIELDS)

    # -- Roles and permissions -----------------------------------------------

    async def list_role_definitions(self, tool_context: ToolContext) -> list[Any]:
        """
        Every role, by key, with the level it's given at (site or org), how
        many times it's assigned, and the keys of the permissions it grants.
        A role's key starts with its level, e.g. org:admin. For the
        permissions' IDs, use get_role_definition.
        """
        roles = await self._call(tool_context, "GET", "/roles")
        return [_role(role) for role in roles or []]

    async def get_role_definition(
        self, role_key: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        One role: its level, name, description, how many times it's
        assigned, and each permission it grants with the permission's ID.

        Args:
            role_key: The role's key, e.g. org:admin, from
                list_role_definitions.
        """
        role = await self._call(tool_context, "GET", f"/roles/{_role_key(role_key)}")
        return _role(role, detailed=True)

    async def list_permission_definitions(self, tool_context: ToolContext) -> list[Any]:
        """
        Every permission, by resource and action: its ID, its key
        (resource:action, e.g. agents:run; "*" stands for any) and what
        it's for. Roles grant permissions by their IDs.
        """
        permissions = await self._call(tool_context, "GET", "/permissions")
        return [pick(permission, PERMISSION_FIELDS) for permission in permissions or []]

    async def get_permission_definition(
        self, permission_id: int, tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        One permission: its key, what it's for, and who last changed it.

        Args:
            permission_id: The permission's ID, from
                list_permission_definitions.
        """
        permission = await self._call(
            tool_context, "GET", f"/permissions/{_permission_id(permission_id)}"
        )
        return pick(permission, PERMISSION_FIELDS + AUDIT_FIELDS)

    async def create_role_definition(
        self,
        key: str,
        name: str,
        tool_context: ToolContext,
        description: str | None = None,
        permission_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """
        Create a role that grants the given permissions. Only site
        administrators can. Once created, it can be given to people at its
        level; its key never changes.

        Args:
            key: Its key: the level it's given at (site or org), a colon and a
                lowercase name, e.g. org:approver.
            name: What people call it, 1 to 200 characters.
            description: What it's for, in a sentence or two.
            permission_ids: The IDs, from list_permission_definitions, of the
                permissions it grants.
        """
        ids = _permission_ids(permission_ids, "permission_ids")
        role = await self._call(
            tool_context,
            "POST",
            "/roles",
            json=given(
                key=key,
                name=name,
                description=description,
                permission_ids=ids or None,
            ),
        )
        return _role(role, detailed=True)

    async def update_role_definition(
        self,
        role_key: str,
        tool_context: ToolContext,
        name: str | None = None,
        description: str | None = None,
        grant_permission_ids: list[int] | None = None,
        revoke_permission_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """
        Rename a role, change its description, or change what it grants:
        grant or revoke permissions, which changes them for everyone who
        holds the role, everywhere. Only site administrators can. Its key
        never changes. The permissions it has and isn't asked to change stay.

        Args:
            role_key: The role's key, e.g. org:admin.
            name: Its new name, 1 to 200 characters.
            description: Its new description; an empty one clears it.
            grant_permission_ids: Permissions to add, by their IDs from
                list_permission_definitions.
            revoke_permission_ids: Permissions to take away, by their IDs
                from get_role_definition.
        """
        path = f"/roles/{_role_key(role_key)}"
        grant = _permission_ids(grant_permission_ids, "grant_permission_ids")
        revoke = _permission_ids(revoke_permission_ids, "revoke_permission_ids")
        if both := sorted(set(grant) & set(revoke)):
            raise ToolFailure(
                f"Permissions {both} are both granted and revoked: name each once."
            )
        body = given(name=name, description=description)
        if not body and not grant and not revoke:
            raise ToolFailure(_NOTHING_TO_CHANGE)
        if grant or revoke:
            # The route replaces what a role grants: send what it grants now,
            # changed as asked.
            role = await self._call(tool_context, "GET", path)
            held = {
                permission["id"]
                for permission in (role or {}).get("permissions") or []
                if isinstance(permission, dict)
                and isinstance(permission.get("id"), int)
            }
            body["permission_ids"] = sorted((held - set(revoke)) | set(grant))
        role = await self._call(tool_context, "PATCH", path, json=body)
        return _role(role, detailed=True)

    async def create_permission_definition(
        self, key: str, tool_context: ToolContext, description: str | None = None
    ) -> dict[str, Any]:
        """
        Create a permission that roles can then be granted. Only site
        administrators can. Forge's own checks use the permissions it came
        with; a new one matters where something checks for its key.

        Args:
            key: Its key, resource:action in lowercase, e.g. reports:read;
                "*" as either part stands for any.
            description: What it allows, in a sentence.
        """
        permission = await self._call(
            tool_context,
            "POST",
            "/permissions",
            json=given(key=key, description=description),
        )
        return pick(permission, PERMISSION_FIELDS + AUDIT_FIELDS)

    async def update_permission_definition(
        self,
        permission_id: int,
        tool_context: ToolContext,
        key: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Change a permission's description or key. Only site administrators
        can. A new key applies in every role that grants it, and Forge checks
        its own permissions by key: renaming one it checks (e.g.
        agents:run) takes that ability from everyone until it's renamed
        back. Say so to the person before renaming one.

        Args:
            permission_id: The permission's ID.
            key: Its new key, resource:action in lowercase.
            description: Its new description; an empty one clears it.
        """
        path = f"/permissions/{_permission_id(permission_id)}"
        body = _changes(key=key, description=description)
        permission = await self._call(tool_context, "PATCH", path, json=body)
        return pick(permission, PERMISSION_FIELDS + AUDIT_FIELDS)

    # -- Users ---------------------------------------------------------------

    async def list_users(
        self,
        tool_context: ToolContext,
        search: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        The people who use Forge, by name: each one's user ID, names, email
        and MS ID (their network ID). Use search to find someone; total says
        how many matched, which can be more than are shown.

        Args:
            search: Only people whose name, email, MS ID or user ID contains
                this, ignoring case.
            limit: How many to show, 1 to 200.
        """
        everyone = await self._call(tool_context, "GET", "/users")
        users = [pick(user, USER_FIELDS) for user in everyone or []]
        if search and search.strip():
            wanted = search.strip().casefold()
            users = [user for user in users if wanted in _searchable(user)]
        shown = users[: max(1, min(limit, MAX_USERS))]
        return {"items": shown, "total": len(users)}

    async def get_user(self, user_id: str, tool_context: ToolContext) -> dict[str, Any]:
        """
        One person: their names, email, MS ID, and who last changed them.

        Args:
            user_id: Their user ID, from the page or list_users.
        """
        user = await self._call(tool_context, "GET", f"/users/{_user_id(user_id)}")
        return pick(user, USER_FIELDS + AUDIT_FIELDS)

    async def create_user(
        self,
        first_name: str,
        last_name: str,
        email: str,
        msid: str,
        tool_context: ToolContext,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Add a person to Forge, so roles can be given to them. Only site
        administrators can. No two people share an email or MS ID.

        Args:
            first_name: Their first name.
            last_name: Their last name.
            email: Their email address.
            msid: Their MS ID, their network user ID: letters, digits, ".",
                "_" and "-".
            user_id: Only to match how they already sign in; a new ID is
                made when omitted, which is usual.
        """
        user = await self._call(
            tool_context,
            "POST",
            "/users",
            json=given(
                id=user_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
                msid=msid,
            ),
        )
        return pick(user, USER_FIELDS + AUDIT_FIELDS)

    async def update_user(
        self,
        user_id: str,
        tool_context: ToolContext,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        msid: str | None = None,
    ) -> dict[str, Any]:
        """
        Change a person's names, email or MS ID. Only site administrators
        can. Their user ID, and so their roles, stay. What's left out stays
        as it is.

        Args:
            user_id: Their user ID, from the page or list_users.
            first_name: Their new first name.
            last_name: Their new last name.
            email: Their new email address.
            msid: Their new MS ID.
        """
        path = f"/users/{_user_id(user_id)}"
        body = _changes(
            first_name=first_name, last_name=last_name, email=email, msid=msid
        )
        user = await self._call(tool_context, "PATCH", path, json=body)
        return pick(user, USER_FIELDS + AUDIT_FIELDS)


def toolset(app: FastAPI, **kwargs: Any) -> AdministrationToolset | None:
    """
    The administration tools, calling ``app``'s routes as each
    conversation's person.

    :param app: The admin API's application.
    :param kwargs: :class:`AdministrationToolset`'s options.
    :return: The toolset, or None without ``jwt_secret``, which the person's
        tokens are signed with.
    """
    if app.state.settings.jwt_secret is None:
        logger.warning(
            "FORGE_ADMIN_JWT_SECRET isn't set: the assistant can't administer "
            "Forge as the people it talks to"
        )
        return None
    return AdministrationToolset(PersonApi.of_app(app), **kwargs)


# -- Checking arguments ------------------------------------------------------

_NOTHING_TO_CHANGE = "Nothing to change: give at least one of the fields to change."


def _node(value: Any, name: str) -> str:
    """An organization's ID, lowercase like the routes'."""
    if not isinstance(value, str) or not NODE_ID.fullmatch(value):
        raise ToolFailure(
            f"{name} must be an ID in UUID form, from the page or a list tool, "
            f"not {value!r}"
        )
    return value.lower()


def _role_key(value: Any) -> str:
    if not isinstance(value, str) or not ROLE_KEY.fullmatch(value):
        raise ToolFailure(
            f"role_key must be a role's key: its level (site or org), a colon "
            f"and a lowercase name, e.g. org:admin; not {value!r}"
        )
    return quote(value, safe="")


def _user_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not SUBJECT.fullmatch(value)
        or ONLY_DOTS.fullmatch(value)
    ):
        raise ToolFailure(f"user_id must be a user ID from list_users, not {value!r}")
    return quote(value, safe="")


def _permission_id(value: Any, name: str = "permission_id") -> int:
    """A permission's ID: a whole number, however the model wrote it."""
    whole = (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, float) and value.is_integer())
        or (isinstance(value, str) and value.isascii() and value.isdigit())
    )
    if not whole or not 0 < int(value) <= MAX_PERMISSION_ID:
        raise ToolFailure(
            f"{name} takes permission IDs, whole numbers from "
            f"list_permission_definitions; not {value!r}"
        )
    return int(value)


def _permission_ids(values: Sequence[Any] | None, name: str) -> list[int]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ToolFailure(f"{name} must be a list of permission IDs, not {values!r}")
    return list(dict.fromkeys(_permission_id(value, name) for value in values))


def _changes(**values: Any) -> dict[str, Any]:
    """A PATCH body: the fields given, at least one."""
    body = given(**values)
    if not body:
        raise ToolFailure(_NOTHING_TO_CHANGE)
    return body


# -- Summaries ---------------------------------------------------------------

AUDIT_FIELDS = ("created_at", "created_by", "updated_at", "updated_by")
ORGANIZATION_FIELDS = ("id", "name", "description")
ROLE_FIELDS = ("key", "level", "name", "description", "member_count")
PERMISSION_FIELDS = ("id", "key", "description")
USER_FIELDS = ("id", "first_name", "last_name", "email", "msid")


def _role(role: Any, detailed: bool = False) -> dict[str, Any]:
    """
    A role: in a list, the keys of what it grants; in detail, each
    permission's ID too, and who last changed it.
    """
    if not isinstance(role, dict):
        return {}
    granted = [p for p in role.get("permissions") or [] if isinstance(p, dict)]
    if not detailed:
        return {
            **pick(role, ROLE_FIELDS),
            "permissions": [p.get("key") for p in granted],
        }
    return {
        **pick(role, ROLE_FIELDS + AUDIT_FIELDS),
        "permissions": [pick(p, PERMISSION_FIELDS) for p in granted],
    }


def _searchable(user: dict[str, Any]) -> str:
    """What a search for a user looks through: their full name, and more."""
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}"
    parts = (name, user.get("email"), user.get("msid"), user.get("id"))
    return " ".join(str(part) for part in parts if part).casefold()
