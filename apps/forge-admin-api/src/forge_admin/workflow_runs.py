"""
Starting runs of an organization's workflows, on the async worker's ``workflows``
queue (apps/forge-async-worker, the workflows task in
packages/python/tasks/workflows).

A run carries the workflow's document as it is when the run starts: editing
the workflow never changes a run in progress. It acts as a member: the
workflows it starts, through this API's service routes
(``api/routes/workflow_service.py``), run as them too. It is one of the
organization's background tasks, labelled with the workflow (and, for a run
another run started, that run's step) so it's found again.

The input is checked here against the fields the workflow's start step
declares, so a mistake is answered at once; the worker checks it again, and
compiles every expression before the first step runs.
"""

import hashlib
import json
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from forge_admin.embedding import WORKFLOWS, Embedding

# The most problems an input refusal names.
MAX_PROBLEMS = 5
WORKFLOW_LABEL = "workflow"
PARENT_LABEL = "parent"


class RunInputError(ValueError):
    """The input doesn't fit the workflow's start step; the message says how."""


def entry_schema(document: dict[str, Any]) -> dict[str, Any]:
    """:return: The input schema the workflow's start step declares; ``{}`` for none."""
    entry = document.get("entry")
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and node.get("id") == entry:
            config = node.get("config")
            schema = config.get("input_schema") if isinstance(config, dict) else None
            return schema if isinstance(schema, dict) else {}
    return {}


def check_input(document: dict[str, Any], value: Any) -> None:
    """
    :raises RunInputError: The value doesn't fit the start step's fields, or
        the workflow has no start step.
    """
    if not any(
        isinstance(node, dict) and node.get("id") == document.get("entry")
        for node in document.get("nodes") or []
    ):
        raise RunInputError("The workflow has no start step")
    problems = misfits(
        entry_schema(document),
        value,
        not_a_schema="The start step's fields aren't a JSON Schema",
    )
    if problems:
        raise RunInputError("The input doesn't fit the start step: " + problems)


def misfits(
    schema: dict[str, Any], value: Any, *, not_a_schema: str, whole: str = "input"
) -> str:
    """
    How a value doesn't fit a JSON Schema (Draft 2020-12), for a refusal.

    :param schema: The schema; ``{}`` takes anything.
    :param value: The value.
    :param not_a_schema: What a refusal says when the schema isn't one.
    :param whole: What a problem with the whole value is said of.
    :return: The first few problems, each with where (``request/id: ...``);
        empty when it fits.
    :raises RunInputError: The schema isn't a JSON Schema.
    """
    if not schema:
        return ""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise RunInputError(f"{not_a_schema}: {error.message}") from None
    validator = Draft202012Validator(schema)
    found = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    return "; ".join(
        f"{'/'.join(map(str, e.absolute_path)) or whole}: {e.message}"
        for e in found[:MAX_PROBLEMS]
    )


def child_key(parent: str) -> str:
    """
    :param parent: The step of the run that starts it: ``<run>/<step key>``.
    :return: The job key of the run it starts, the same however often the
        step asks, so a retried start is one run.
    """
    return f"workflows.run:child:{hashlib.sha256(parent.encode()).hexdigest()[:32]}"


async def start_run(
    embedding: Embedding,
    record: dict[str, Any],
    *,
    input: Any,
    run_as: str,
    run_as_name: str,
    trigger: dict[str, Any],
    parent: str | None = None,
    depth: int = 0,
) -> dict[str, str]:
    """
    Submit a run of a workflow, as its record is now.

    :param record: The workflow's record (``WorkflowStore``).
    :param input: What the run starts with; checked already.
    :param run_as: The member it acts as.
    :param run_as_name: Their name, for its record.
    :param trigger: What started it: ``{"type": "manual" | "workflow", ...}``.
    :param parent: The run and step that start it, for a run another started.
    :param depth: How many runs deep it is.
    :return: ``{"queue", "key"}`` of its job.
    :raises EmbeddingError: The queue could not be reached.
    """
    key = (
        child_key(parent) if parent else f"workflows.run:{record['_id']}:{uuid4().hex}"
    )
    labels = {WORKFLOW_LABEL: record["_id"]}
    if parent:
        labels[PARENT_LABEL] = parent
    # The document as JSON (MongoDB hands back its own types).
    document = json.loads(json.dumps(record["document"], default=str))
    payload = {
        "tenant_id": record["organization_id"],
        "workflow_id": record["_id"],
        "revision": record["revision"],
        "name": str(document.get("name") or ""),
        "document": document,
        "input": input,
        "run_as": run_as,
        "run_as_name": run_as_name,
        "depth": depth,
        "parent": parent,
        "trigger": trigger,
    }
    await embedding.run_workflow(
        tenant_id=record["organization_id"],
        key=key,
        labels=labels,
        payload=payload,
        requested_by={"id": run_as, "display_name": run_as_name},
    )
    return {"queue": WORKFLOWS, "key": key}
