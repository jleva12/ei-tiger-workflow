"""Every kit and toolset ``helpers.yaml`` can name, and loading it over them.

A new helper that takes ``{ask, context}`` and answers in text needs only
an entry in ``helpers.yaml`` (the ``text`` kit). One that reads its own
input or makes something needs a kit: add it to :data:`KITS`. A toolset
any helper may be given goes in :data:`TOOLSETS`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from forge_admin.helpers import organization, text
from forge_admin.helpers.config import HELPERS_FILE, HelperConfig
from forge_admin.helpers.executor import Helpers
from forge_admin.helpers.kit import HelperToolset, Kit
from forge_admin.instruction_writer import editor as instruction_editor
from forge_admin.instruction_writer import kit as instruction_writer
from forge_admin.skills import creator as skill_creator

KITS: tuple[Kit[Any], ...] = (
    text.KIT,
    instruction_writer.KIT,
    instruction_editor.KIT,
    skill_creator.KIT,
)

TOOLSETS: tuple[HelperToolset, ...] = (
    organization.TOOLSET,
    skill_creator.VAULT_TOOLSET,
)


def load(path: Path | None = None, *, models: Iterable[str] | None = None) -> Helpers:
    """
    The helpers from ``path`` (the bundled ``helpers.yaml`` by default).

    :param models: The models the assistant may use, which the helpers may
        name; None not to check.
    :raises ValueError: The file isn't a valid configuration, or names a
        kit, toolset, tool or model that doesn't exist.
    """
    return Helpers(
        HelperConfig.load(path or HELPERS_FILE), KITS, TOOLSETS, models=models
    )
