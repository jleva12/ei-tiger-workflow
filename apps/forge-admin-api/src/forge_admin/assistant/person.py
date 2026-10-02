"""Who the assistant is talking to: from their sign-in, never from the browser.

The ``/agents`` routes have already checked that the conversation's user is
the signed-in person. Each run looks them up here and hands them to the agent
as the session state ``temp:person`` (``PERSON``), which lasts for the run and
is never stored, and which the web console can't set (see ``CLIENT_STATE`` in
the routes). ``describe_person`` puts them in the agent's instruction.
"""

import json
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from forge_admin.auth.access import Level, Scope, organization_memberships
from forge_admin.auth.authorization import assignments_of
from forge_admin.db.session import create_sessionmaker
from forge_admin.models import User

# The session state key the person is handed to the agent under: ``temp:``,
# so it's gone after the run and looked up again for the next.
PERSON = "temp:person"
# Organizations listed in the instruction; past it, how many more.
MAX_ORGANIZATIONS = 30


class PersonOrganization(BaseModel):
    """An organization the person is a member of, and their roles in it."""

    id: str
    name: str
    roles: list[str]


class PersonRole(BaseModel):
    """A role the person holds outside their organizations, e.g. ``site:admin``."""

    role: str
    # ``site``.
    scope: str


class Person(BaseModel):
    """The signed-in person, as the agent is told about them."""

    id: str
    name: str | None = None
    email: str | None = None
    organizations: list[PersonOrganization] = Field(default_factory=list)
    roles: list[PersonRole] = Field(default_factory=list)


# Looks a person up by their user ID; None when they can't be.
PersonLookup = Callable[[str], Awaitable[Person | None]]


def database_people(engine: AsyncEngine) -> PersonLookup:
    """
    Look people up in the admin database: their profile, their
    organizations and the roles they hold site-wide.

    :param engine: The admin database's engine.
    :return: The lookup. A subject that isn't a user (e.g. a service) is
        still a person, with only its ID and roles.
    """
    sessions = create_sessionmaker(engine)

    async def lookup(user_id: str) -> Person | None:
        async with sessions() as session:
            user = await session.get(User, user_id)
            memberships = await organization_memberships(session, user_id)
            assignments = await assignments_of(session, user_id)
        roles = [
            PersonRole(role=rule.v1 or "", scope=str(scope))
            for rule in assignments
            if (scope := Scope.from_pattern(rule.v2 or "")).level is not Level.ORG
        ]
        return Person(
            id=user_id,
            name=f"{user.first_name} {user.last_name}".strip() if user else None,
            email=user.email if user else None,
            organizations=[
                PersonOrganization(
                    id=membership.organization.id,
                    name=membership.organization.name,
                    roles=membership.roles,
                )
                for membership in memberships
            ],
            roles=roles,
        )

    return lookup


def quote(text: str) -> str:
    """
    Text for the instruction as a JSON string: quoted, with line breaks and
    quotes escaped, so a name can't pass for the instruction's own words.

    :param text: E.g. an organization's name.
    :return: The quoted text.
    """
    quoted = json.dumps(text, ensure_ascii=False)
    # Lone surrogates can't be sent to the model as UTF-8.
    return quoted.encode("utf-8", "replace").decode("utf-8")


def _organization(organization: PersonOrganization) -> str:
    roles = ", ".join(organization.roles)
    return f"{quote(organization.name)} (organization {organization.id}) as {roles}"


def describe_person(value: object) -> str | None:
    """
    The instruction's section on who the person is.

    :param value: The session state ``temp:person``.
    :return: The section, or None when the run has no person (e.g. the
        lookup failed).
    """
    if value is None:
        return None
    person = Person.model_validate(value)
    who = quote(person.name) if person.name else "A signed-in subject"
    email = f" ({quote(person.email)})" if person.email else ""
    lines = [
        "Who you're talking to (from their sign-in, so reliable):",
        f"- {who}{email}, user ID {quote(person.id)}.",
    ]
    if person.organizations:
        listed = person.organizations[:MAX_ORGANIZATIONS]
        more = len(person.organizations) - len(listed)
        lines.append(
            "- Their organizations: "
            + "; ".join(_organization(organization) for organization in listed)
            + (f"; and {more} more." if more else ".")
        )
    else:
        lines.append("- They aren't a member of any organization.")
    if person.roles:
        lines.append(
            "- Roles they hold site-wide: "
            + "; ".join(f"{role.role} in {role.scope}" for role in person.roles)
            + "."
        )
    return "\n".join(lines)
