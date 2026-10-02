"""Nodes' ADK names, and the hidden nodes' the build adds."""

import re
import unicodedata

# The graph's name when the agent's makes no ADK name.
GRAPH_NAME = "agent"
# ADK keeps this name for the person.
RESERVED_NAMES = frozenset({"user"})
# The longest ADK name, as the builder cuts them.
MAX_NAME = 48

# The hidden nodes. An ADK name never starts with "_" nor has "__" in it, so
# these are never a node's.
#: Where every ending of an agent's graph leads: its one output.
FINISH_NODE = "__finish__"
#: Where the endings that aren't End steps lead, before the finish.
ENDED_NODE = "__ended__"
#: Where a loop body's ways back to its loop lead: the item's result.
BACK_NODE = "__back__"
#: Where a loop body's other endings lead: they hand on nothing.
STOP_NODE = "__stop__"


def adk_name(name: str) -> str:
    """
    A node's or sub-agent's ADK name: its name as a Python identifier, as the
    web's ``slugify`` makes it (``Billing specialist``: ``billing_specialist``).

    :param name: Its name.
    :return: The ADK name; empty when the name has no letter to start one.
    """
    slug = unicodedata.normalize("NFKD", name.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"^_+|_+$", "", slug)
    slug = re.sub(r"^[^a-z]+", "", slug)
    return slug[:MAX_NAME]


def body_name(loop: str) -> str:
    """:return: The ADK name of a loop's body graph."""
    return f"{loop}__each"


def via_name(merge: str, source: str) -> str:
    """:return: The ADK name of the hidden node that tags what a step hands a
    Merge "any" with the step's ID."""
    return f"{merge}__via__{source}"
