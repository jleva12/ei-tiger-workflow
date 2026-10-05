"""Nodes' ADK names, as the builder makes them."""

import re
import unicodedata

#: ADK keeps this name for the person.
RESERVED_NAMES = frozenset({"user"})
#: The longest ADK name, as the builder cuts them.
MAX_NAME = 48


def adk_name(name: str) -> str:
    """
    A node's ADK name (an agent's, an HTTP tool's): its name as a Python
    identifier, as the builder's ``slugify`` makes it
    (``Billing specialist``: ``billing_specialist``).

    :param name: Its name.
    :return: The ADK name; empty when the name has no letter to start one.
    """
    slug = unicodedata.normalize("NFKD", name.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"^_+|_+$", "", slug)
    slug = re.sub(r"^[^a-z]+", "", slug)
    return slug[:MAX_NAME]
