"""A system design knowledge base as its agents read it: what it's about,
its applications, the other parts of the system (databases, queues,
external systems…), the groups they're in and how they connect. In one line
for the knowledge base tool's description (:func:`system_summary`), and in
full, or around one part of it, for the tool that reads the design
(:func:`design_view`). The admin API's tools and the async worker's runs
describe it the same way.

A design, as the admin API reads it (``forge_admin.knowledge.designs``) and
a run's snapshot carries it, is ``{groups, applications, components,
connections}``: each group ``{name, description}``; each application
``{name, url, group}`` (``owner/name``); each component ``{name, kind,
technology, description, group}``; each connection ``{source, kind, target,
description}``, the ends by name. A ``group`` is the group's name, or empty.
"""

from collections.abc import Mapping, Sequence
from typing import Any

#: Each connection kind as a verb from one part of the system to another.
CONNECTION_VERBS: Mapping[str, str] = {
    "connects_to": "connects to",
    "calls": "calls the API of",
    "depends_on": "depends on",
    "events": "sends events to",
    "shares_data": "shares data with",
    "reads_from": "reads from",
    "writes_to": "writes to",
    "reads_writes": "reads and writes",
    "publishes_to": "publishes to",
    "consumes_from": "consumes from",
}

#: Each component kind as agents are told it.
COMPONENT_KINDS: Mapping[str, str] = {
    "database": "database",
    "queue": "queue or topic",
    "cache": "cache",
    "storage": "storage",
    "external": "external system",
    "service": "service",
    "client": "client",
}

#: The most connections one description lists; the rest are counted.
MAX_DESCRIBED = 40
#: The most components and groups one description lists; the rest are counted.
MAX_PARTS = 30
#: How much of a group's description one description keeps.
MAX_NOTE = 200


def _note(text: str, limit: int | None = None) -> str:
    note = " ".join(text.split())
    if limit is not None and len(note) > limit:
        return note[: limit - 1].rstrip() + "…"
    return note


def connection_sentence(
    source: str, kind: str, target: str, description: str = ""
) -> str:
    """:return: e.g. ``acme/web calls the API of acme/orders (POST /v1/orders)``."""
    verb = CONNECTION_VERBS.get(kind, kind.replace("_", " "))
    note = _note(description)
    return f"{source} {verb} {target}" + (f" ({note})" if note else "")


def component_phrase(component: Mapping[str, Any]) -> str:
    """:return: e.g. ``orders-db (database, PostgreSQL 16)``."""
    kind = str(component.get("kind") or "")
    about = [COMPONENT_KINDS.get(kind, kind.replace("_", " "))]
    technology = _note(str(component.get("technology") or ""))
    if technology:
        about.append(technology)
    return f"{component.get('name', '')} ({', '.join(a for a in about if a)})"


def members_of(design: Mapping[str, Any]) -> dict[str, list[str]]:
    """:return: Each group's applications and components, by name, by the
    group's name: the applications first, as listed."""
    members: dict[str, list[str]] = {
        str(g.get("name") or ""): [] for g in design.get("groups") or ()
    }
    for part in [
        *(design.get("applications") or ()),
        *(design.get("components") or ()),
    ]:
        group = str(part.get("group") or "")
        if group in members:
            members[group].append(str(part.get("name") or ""))
    return members


def _listed(phrases: Sequence[str], limit: int) -> str:
    more = len(phrases) - limit
    return "; ".join(phrases[:limit]) + (f"; and {more} more" if more > 0 else "")


def system_summary(
    description: str,
    applications: Sequence[str],
    connections: Sequence[Mapping[str, str]],
    *,
    components: Sequence[Mapping[str, Any]] = (),
    groups: Sequence[Mapping[str, Any]] = (),
    members: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """
    :param description: The knowledge base's own description.
    :param applications: Its applications' names, e.g. ``acme/orders``.
    :param connections: Each ``{source, kind, target, description}``, the
        ends by name.
    :param components: Its other parts, each ``{name, kind, technology}``.
    :param groups: Its groups, each ``{name, description}``.
    :param members: What's in each group, by name (:func:`members_of`).
    :return: Its description, the applications whose code it searches, its
        other parts, its groups and how they all connect.
    """
    code = f"code of {', '.join(applications)}" if applications else "code repositories"
    parts = [description.strip(), code]
    if components:
        parts.append(
            "other parts: "
            + _listed([component_phrase(c) for c in components], MAX_PARTS)
        )
    if groups:
        listed = []
        for group in groups:
            name = str(group.get("name") or "")
            inside = (members or {}).get(name) or ()
            # Its own full stop would run into the list's semicolon.
            note = _note(str(group.get("description") or ""), MAX_NOTE).rstrip(".")
            listed.append(
                name
                + (f" ({', '.join(inside)})" if inside else "")
                + (f": {note}" if note else "")
            )
        parts.append("groups: " + _listed(listed, MAX_PARTS))
    if connections:
        parts.append(
            "how they connect: "
            + _listed(
                [
                    connection_sentence(
                        c.get("source", ""),
                        c.get("kind", ""),
                        c.get("target", ""),
                        c.get("description", ""),
                    )
                    for c in connections
                ],
                MAX_DESCRIBED,
            )
        )
    return "; ".join(p for p in parts if p)


def design_summary(description: str, design: Mapping[str, Any]) -> str:
    """:return: :func:`system_summary` of a whole design."""
    return system_summary(
        description,
        [str(a.get("name") or "") for a in design.get("applications") or ()],
        list(design.get("connections") or ()),
        components=list(design.get("components") or ()),
        groups=list(design.get("groups") or ()),
        members=members_of(design),
    )


def _found(names: Sequence[str], focus: str) -> set[str]:
    """The names a focus picks: those it equals (ignoring case), else those
    it's part of."""
    wanted = focus.strip().casefold()
    exact = {n for n in names if n.casefold() == wanted}
    return exact or {n for n in names if wanted in n.casefold()}


def _view(
    knowledge_base: str,
    description: str,
    design: Mapping[str, Any],
    focus: str,
) -> dict[str, Any] | None:
    """One system's design for the tool that reads it, or only the parts a
    focus picks and what they connect to; None when it picks nothing."""
    members = members_of(design)
    groups = [
        {
            "name": str(g.get("name") or ""),
            "description": _note(str(g.get("description") or "")),
            "members": members.get(str(g.get("name") or ""), []),
        }
        for g in design.get("groups") or ()
    ]
    applications = [
        {
            "name": str(a.get("name") or ""),
            "url": str(a.get("url") or ""),
            "group": str(a.get("group") or ""),
        }
        for a in design.get("applications") or ()
    ]
    components = [
        {
            "name": str(c.get("name") or ""),
            "kind": COMPONENT_KINDS.get(
                str(c.get("kind") or ""), str(c.get("kind") or "")
            ),
            "technology": str(c.get("technology") or ""),
            "description": str(c.get("description") or ""),
            "group": str(c.get("group") or ""),
        }
        for c in design.get("components") or ()
    ]
    connections = [
        {
            "source": str(c.get("source") or ""),
            "relation": CONNECTION_VERBS.get(
                str(c.get("kind") or ""), str(c.get("kind") or "")
            ),
            "target": str(c.get("target") or ""),
            "description": _note(str(c.get("description") or "")),
        }
        for c in design.get("connections") or ()
    ]
    if focus.strip():
        picked = _found([str(p["name"]) for p in [*applications, *components]], focus)
        picked_groups = _found([str(g["name"]) for g in groups], focus)
        for group in picked_groups:
            picked |= set(members.get(group, ()))
        if not picked and not picked_groups:
            return None
        connections = [
            c for c in connections if c["source"] in picked or c["target"] in picked
        ]
        # The other ends, so each connection's two ends are described.
        shown = (
            picked
            | {c["source"] for c in connections}
            | {c["target"] for c in connections}
        )
        applications = [a for a in applications if a["name"] in shown]
        components = [c for c in components if c["name"] in shown]
        groups = [
            g
            for g in groups
            if g["name"] in picked_groups or any(m in shown for m in g["members"])
        ]
    return {
        "knowledge_base": knowledge_base,
        "description": description,
        "groups": groups,
        "applications": applications,
        "components": components,
        "connections": connections,
    }


def design_view(
    systems: Sequence[tuple[str, str, Mapping[str, Any]]], focus: str = ""
) -> dict[str, Any]:
    """
    What the tool that reads system designs answers.

    :param systems: Each knowledge base's name, description and design.
    :param focus: An application's, component's or group's name (or part of
        it): only that part (a group: what's in it), what it connects to,
        and how; every system whole when empty.
    :return: ``{systems: [...]}``, each with its ``knowledge_base``,
        ``description``, ``groups`` (``members`` by name), ``applications``,
        ``components`` and ``connections`` (``source``, ``relation``,
        ``target``, ``description``); and a ``note`` listing the names when
        the focus picks nothing.
    """
    found = [
        view
        for name, description, design in systems
        if (view := _view(name, description, design, focus)) is not None
    ]
    if found or not focus.strip():
        return {"systems": found}
    names = sorted(
        {
            str(p.get("name") or "")
            for _, _, design in systems
            for key in ("applications", "components", "groups")
            for p in design.get(key) or ()
        },
        key=str.casefold,
    )
    return {
        "systems": [],
        "note": f"Nothing is named {focus.strip()!r}. The parts are: "
        + (", ".join(names) or "none yet")
        + ". Call again with one of them, or without focus for everything.",
    }
