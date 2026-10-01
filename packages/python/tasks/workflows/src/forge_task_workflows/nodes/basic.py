"""The steps that only read and route the run's data: Start, If, Switch,
Match, Transform and Delay."""

from __future__ import annotations

from datetime import datetime, timedelta

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from forge_task_workflows.engine import Outcome, StepRun
from forge_task_workflows.errors import StepFailed, WorkflowFailed
from forge_task_workflows.expressions import as_text

UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}
MAX_PROBLEMS = 5


def fit_problems(value: object, schema: dict, what: str) -> list[str]:
    """Why ``value`` doesn't fit ``schema`` (at most a few reasons), or nothing."""
    try:
        validator = Draft202012Validator(schema)
    except SchemaError as exc:
        return [f"its {what} schema isn't a JSON Schema: {exc.message}"]
    found = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or what}: {e.message}" for e in found[:MAX_PROBLEMS]]


async def entry(step: StepRun) -> Outcome:
    """The run's input, checked against the fields the start step declares."""
    schema = step.settings.input_schema
    value = step.run.input
    if schema:
        problems = fit_problems(value, schema, "input")
        if problems:
            raise WorkflowFailed("The run's input doesn't fit its start step: " + "; ".join(problems))
    return Outcome("next", value)


async def if_(step: StepRun) -> Outcome:
    branch = "true" if step.truthy(step.settings.condition) else "false"
    return Outcome(branch, {"branch": branch})


async def switch(step: StepRun) -> Outcome:
    """The case whose value the expression equals, as text; else Default."""
    value = as_text(step.evaluate(step.settings.value, what="What it switches on"))
    for case in step.settings.cases:
        if case.value == value:
            return Outcome(case.id, {"branch": case.id})
    return Outcome("default", {"branch": "default"})


async def match(step: StepRun) -> Outcome:
    """The first rule whose condition is true; else Otherwise."""
    for index, arm in enumerate(step.settings.arms):
        if step.truthy(arm.condition, what=f"The condition of {arm.label or f'rule {index + 1}'}"):
            return Outcome(arm.id, {"branch": arm.id})
    return Outcome("otherwise", {"branch": "otherwise"})


async def transform(step: StepRun) -> Outcome:
    """What its expression makes, held to its declared output when it has one."""
    value = step.evaluate(step.settings.expression)
    schema = step.settings.output_schema
    if schema:
        problems = fit_problems(value, schema, "output")
        if problems:
            raise StepFailed("What it made doesn't fit its declared output: " + "; ".join(problems))
    return Outcome("next", value)


async def delay(step: StepRun) -> Outcome:
    """Waits, then goes on. A short wait waits in place; a long one lets the worker go."""
    services = step.services
    progress = step.progress
    if "until" not in progress:
        seconds = step.settings.amount * UNIT_SECONDS[step.settings.unit]
        until = services.clock() + timedelta(seconds=max(0.0, seconds))
        await step.keep(until=until.isoformat())
    else:
        until = datetime.fromisoformat(progress["until"])
    remaining = (until - services.clock()).total_seconds()
    if remaining > services.settings.inline_delay_seconds:
        await step.wait_until(until, reason=f"a delay of {step.settings.amount:g} {step.settings.unit}")
    if remaining > 0:
        await services.sleep(remaining)
    return Outcome("next", {"resumed_at": services.clock().isoformat()})
