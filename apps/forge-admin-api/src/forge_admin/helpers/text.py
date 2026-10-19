"""The ``text`` kit: for a helper that needs no Python of its own.

It takes what the person asks and, optionally, what the page knows (any
JSON object), sends both, and gives back the helper's last answer as
``{"text": …}``. A helper with this kit is all configuration: its
instruction, toolsets and limits in ``helpers.yaml``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from forge_admin.helpers.kit import Finished, HelperRun, Kit, Outcome

#: The most context a request carries, as JSON.
CONTEXT_CHARS = 60_000


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What the person wants.
    ask: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ]
    #: What the page knows that the helper should: the record being set up, say.
    context: dict[str, Any] | None = None

    @field_validator("context")
    @classmethod
    def _small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value)) > CONTEXT_CHARS:
            raise ValueError(f"at most {CONTEXT_CHARS} characters of JSON")
        return value


async def prepare(run: HelperRun, given: TextInput, session: AsyncSession) -> str:
    parts = []
    if given.context:
        context = json.dumps(given.context, ensure_ascii=False, indent=1)
        parts += ["# What the page shows", f"```json\n{context}\n```"]
    parts += ["# What the person wants", given.ask]
    return "\n\n".join(parts)


async def finish(run: HelperRun, given: TextInput, outcome: Outcome) -> Finished:
    return Finished({"text": outcome.text})


KIT = Kit(name="text", input=TextInput, prepare=prepare, finish=finish)
