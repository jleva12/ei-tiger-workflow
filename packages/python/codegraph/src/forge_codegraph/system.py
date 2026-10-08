"""A system design knowledge base as its agents read it: what it's about,
its applications, and how they connect, in one line for the knowledge base
tool's description. The admin API's tools and the async worker's runs
describe it the same way."""

from collections.abc import Mapping, Sequence

#: Each connection kind as a verb between two applications.
CONNECTION_VERBS: Mapping[str, str] = {
    "connects_to": "connects to",
    "calls": "calls the API of",
    "depends_on": "depends on",
    "events": "sends events to",
    "shares_data": "shares data with",
}

#: The most connections one description lists; the rest are counted.
MAX_DESCRIBED = 40


def connection_sentence(
    source: str, kind: str, target: str, description: str = ""
) -> str:
    """:return: e.g. ``acme/web calls the API of acme/orders (POST /v1/orders)``."""
    verb = CONNECTION_VERBS.get(kind, kind.replace("_", " "))
    note = " ".join(description.split())
    return f"{source} {verb} {target}" + (f" ({note})" if note else "")


def system_summary(
    description: str,
    applications: Sequence[str],
    connections: Sequence[Mapping[str, str]],
) -> str:
    """
    :param description: The knowledge base's own description.
    :param applications: Its applications' names, e.g. ``acme/orders``.
    :param connections: Each ``{source, kind, target, description}``, the
        ends by name.
    :return: Its description, the applications whose code it searches, and
        how they connect.
    """
    code = f"code of {', '.join(applications)}" if applications else "code repositories"
    parts = [description.strip(), code]
    if connections:
        listed = [
            connection_sentence(
                c.get("source", ""),
                c.get("kind", ""),
                c.get("target", ""),
                c.get("description", ""),
            )
            for c in connections[:MAX_DESCRIBED]
        ]
        more = len(connections) - len(listed)
        parts.append(
            "how they connect: "
            + "; ".join(listed)
            + (f"; and {more} more" if more > 0 else "")
        )
    return "; ".join(p for p in parts if p)
