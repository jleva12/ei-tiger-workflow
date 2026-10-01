"""How long a model thinks before it answers: the levels a model offers.

Pi's rules (``getSupportedThinkingLevels`` and ``clampThinkingLevel`` in
``@earendil-works/pi-ai``), so the admin API's assistant and workflows' agent
steps offer a model the same levels: a model without ``reasoning`` only has
``off``; a reasoning model has ``off`` through ``high``, and ``xhigh`` when its
``thinkingLevelMap`` maps it; a level mapped to null is left out.
"""

from typing import Literal

from forge_common.model_provider.config import ConfiguredModel

__all__ = [
    "THINKING_LEVELS",
    "ThinkingLevel",
    "clamp_thinking_level",
    "supported_thinking_levels",
    "thinking_value",
]

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
# From least to most thinking.
THINKING_LEVELS: tuple[ThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)
# Offered only when the model's map names them.
_MAPPED_ONLY = frozenset({"xhigh"})


def supported_thinking_levels(model: ConfiguredModel) -> list[ThinkingLevel]:
    """
    :param model: A configured model.
    :return: The levels it offers, from least to most thinking.
    """
    if not model.reasoning:
        return ["off"]
    mapping = model.thinking_level_map
    mapped = mapping.model_fields_set if mapping else set()
    levels: list[ThinkingLevel] = []
    for level in THINKING_LEVELS:
        value = getattr(mapping, level) if mapping else None
        if level in mapped and value is None:
            continue
        if level in _MAPPED_ONLY and level not in mapped:
            continue
        levels.append(level)
    return levels


def clamp_thinking_level(model: ConfiguredModel, level: str) -> ThinkingLevel:
    """
    The level a model runs at when asked for ``level``: that one if it offers
    it, else the nearest above, else the nearest below.

    :param model: A configured model.
    :param level: A thinking level, e.g. ``high``.
    :return: One of the model's levels; its least for an unknown level.
    """
    available = supported_thinking_levels(model)
    known = [each for each in THINKING_LEVELS if each == level]
    if not known:
        return available[0]
    index = THINKING_LEVELS.index(known[0])
    # The level itself, then those above it, then those below.
    for each in (*THINKING_LEVELS[index:], *reversed(THINKING_LEVELS[:index])):
        if each in available:
            return each
    return available[0]


def thinking_value(model: ConfiguredModel, level: ThinkingLevel) -> str | None:
    """
    What a level means to the model's provider: its ``thinkingLevelMap`` entry,
    or else the level's own name (``off`` has none: the provider's default).

    :param model: A configured model.
    :param level: One of its levels.
    :return: e.g. ``high`` for an OpenAI reasoning effort; None for ``off``
        unless the map names a value for it.
    """
    mapping = model.thinking_level_map
    if mapping is not None and level in mapping.model_fields_set:
        return getattr(mapping, level)
    return None if level == "off" else level
