"""The repositories a caller of the code graph's tools reads: its
organization's, as the admin API answers them (``GET /code-graph/access``,
or ``/organizations/{id}/code-graph/access`` for a person in one).

The graph has one repository per GitHub URL, whose ID follows from the URL,
so an organization reads the graphs of its repositories' URLs and nothing
else, cross-repository links included. The same answer carries how the
organization's repositories connect, as people drew them on its system
design knowledge bases' maps, each of those knowledge bases' whole design,
and who keeps its cross-repository links in the graph (``kb:<id>``): a graph
shared with another organization holds that one's links too, which are left
out.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from forge_codegraph_query import NotFound, graph_id

#: What a caller needs in the organization: reading its repositories.
READ = "repositories:read"


def repository_id(url: str) -> str:
    """The graph's ID of the repository at a GitHub URL, as the worker names
    it (``https://github.com/<owner>/<name>``, lowercase)."""
    return graph_id("repo", url)


@dataclass(frozen=True)
class RepositoryScope:
    """
    The repositories a caller reads: its organization's.

    :ivar subject: Who's calling, as the admin API names them (a user's ID,
        or ``apikey:<id>``).
    :ivar organization_id: Their organization.
    :ivar repository_ids: The graph's IDs of the organization's repositories.
    :ivar link_owners: Who keeps the organization's cross-repository links
        (``kb:<id>``); None follows every link, as before the admin API said.
    :ivar connections: How its repositories connect, as drawn on its system
        maps: each ``{id, knowledge_base_id, knowledge_base, kind,
        description, source, target, code_links}``, the ends with their
        graph ``repository_id``.
    :ivar systems: Its system design knowledge bases' designs: each ``{id,
        name, description, groups, applications, components,
        connections}`` (``forge_codegraph.system``), each application with
        its graph ``repository_id``.
    """

    subject: str
    organization_id: str
    repository_ids: frozenset[str]
    link_owners: frozenset[str] | None = field(default=None, compare=False)
    connections: tuple[dict[str, Any], ...] = field(default=(), compare=False)
    systems: tuple[dict[str, Any], ...] = field(default=(), compare=False)

    @classmethod
    def from_access(cls, answer: Mapping[str, Any]) -> "RepositoryScope":
        """
        The scope the admin API's access answer gives.

        :raises KeyError, TypeError, ValueError: The answer isn't one.
        """
        owners = answer.get("link_owners")
        return cls(
            subject=str(answer["subject"]),
            organization_id=str(answer["organization_id"]),
            repository_ids=frozenset(
                repository_id(str(r["url"])) for r in answer.get("repositories") or ()
            ),
            # An admin API from before system maps doesn't say; then every
            # link is followed, as it was.
            link_owners=(
                frozenset(str(o) for o in owners or ())
                if "link_owners" in answer
                else None
            ),
            connections=tuple(_connection(c) for c in answer.get("connections") or ()),
            systems=tuple(_system(s) for s in answer.get("systems") or ()),
        )

    def claims(self) -> dict[str, Any]:
        """The scope as JSON claims, to carry on a verified credential."""
        claims: dict[str, Any] = {
            "sub": self.subject,
            "org_id": self.organization_id,
            "repository_ids": sorted(self.repository_ids),
            "connections": list(self.connections),
            "systems": list(self.systems),
        }
        if self.link_owners is not None:
            claims["link_owners"] = sorted(self.link_owners)
        return claims

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any], subject: str) -> "RepositoryScope":
        """The scope :meth:`claims` carried."""
        owners = claims.get("link_owners")
        return cls(
            subject=subject,
            organization_id=str(claims.get("org_id") or ""),
            repository_ids=frozenset(claims.get("repository_ids") or ()),
            link_owners=frozenset(owners) if owners is not None else None,
            connections=tuple(claims.get("connections") or ()),
            systems=tuple(claims.get("systems") or ()),
        )

    def owns(self, owner: str) -> bool:
        """Whether a cross-repository link is the organization's: drawn on
        one of its system maps."""
        return self.link_owners is None or owner in self.link_owners

    def readable(self, repository: str) -> bool:
        """Whether the caller reads a repository; for following links out of
        the one asked about."""
        return repository in self.repository_ids

    def check(self, *repositories: str) -> None:
        """
        :raises NotFound: The caller doesn't read one of them; worded as the
            graph words a repository that isn't there, so it tells nothing
            about other organizations' repositories.
        """
        for repository in repositories:
            if repository not in self.repository_ids:
                raise NotFound(f"repository {repository}")


def _connection(found: Mapping[str, Any]) -> dict[str, Any]:
    """A connection as the admin API answered it, each application with its
    graph's repository ID."""

    def end(application: Mapping[str, Any]) -> dict[str, str]:
        url = str(application["url"])
        return {
            "repository_id": repository_id(url),
            "name": f"{application.get('owner', '')}/{application.get('name', '')}",
        }

    return {
        "id": str(found["id"]),
        "knowledge_base_id": str(found.get("knowledge_base_id") or ""),
        "knowledge_base": str(found.get("knowledge_base") or ""),
        "kind": str(found["kind"]),
        "description": str(found.get("description") or ""),
        "source": end(found["source"]),
        "target": end(found["target"]),
        "code_links": [
            {
                "label": str(link.get("label") or ""),
                "source": dict(link["source"]),
                "target": dict(link["target"]),
            }
            for link in found.get("code_links") or ()
        ],
    }


def _system(found: Mapping[str, Any]) -> dict[str, Any]:
    """A system design knowledge base's design as the admin API answered it,
    each application with its graph's repository ID."""
    return {
        "id": str(found["id"]),
        "name": str(found.get("name") or ""),
        "description": str(found.get("description") or ""),
        "groups": [dict(g) for g in found.get("groups") or ()],
        "applications": [
            {**a, "repository_id": repository_id(str(a["url"]))}
            for a in found.get("applications") or ()
        ],
        "components": [dict(c) for c in found.get("components") or ()],
        "connections": [dict(c) for c in found.get("connections") or ()],
    }
