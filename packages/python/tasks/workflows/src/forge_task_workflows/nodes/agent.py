"""Agent: a conversation with the step's model (services/llm.py)."""

from __future__ import annotations

import json
from typing import Any

from forge_task_workflows.engine import Outcome, StepRun
from forge_task_workflows.errors import StepFailed
from forge_task_workflows.nodes.basic import fit_problems


async def agent(step: StepRun) -> Outcome:
    s = step.settings
    llm = step.services.require_llm()
    model = llm.model_for(s.model.provider, s.model.name)
    instructions = step.render(s.instructions, what="Its instructions").strip()
    if not instructions:
        raise StepFailed("It has no instructions")
    schema: dict[str, Any] | None = None
    if s.output == "json":
        try:
            schema = json.loads(s.output_schema) if s.output_schema.strip() else {"type": "object"}
        except json.JSONDecodeError as exc:
            raise StepFailed(f"Its output schema isn't JSON: {exc}") from exc
    previous = step.token.previous
    prompt = "Do what your instructions say." + (
        f"\n\nWhat the step before handed on:\n{json.dumps(previous, ensure_ascii=False, default=str)[:20000]}"
        if previous is not None
        else ""
    )
    progress = step.progress

    async def keep(history: list[dict[str, Any]], turns: int) -> None:
        await step.keep(history=history, turns=turns)

    answer = await llm.converse(
        model=model,
        thinking_level=s.thinking_level.strip() or None,
        instructions=instructions,
        prompt=prompt,
        tools=[],
        output_schema=schema,
        max_turns=step.services.settings.agent_turn_cap,
        history=progress.get("history"),
        turns=int(progress.get("turns", 0)),
        keep=keep,
    )
    if schema is not None:
        problems = fit_problems(answer, schema, "answer")
        if problems:
            raise StepFailed("Its answer doesn't fit its output schema: " + "; ".join(problems))
    return Outcome("next", answer)
