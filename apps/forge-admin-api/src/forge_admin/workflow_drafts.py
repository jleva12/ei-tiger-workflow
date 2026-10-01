"""
Drafts of organizations' workflows, as the assistant writes them from what a
person describes.

:func:`complete` makes a draft a whole ``forge.workflow/v1`` document from
the step catalog (``workflow.catalog.json``, which the web console's
``npm run generate:workflow-schema`` generates from the builder's model):
each step's settings over its kind's defaults, its ways out, and every
connection's ID, so a draft need only say what differs. :func:`problems`
then checks it the ways it would be checked when saved and run:

- the format's JSON Schema, as the workflows routes check every save;
- as the runner reads it (``forge_task_workflows``): each step's settings
  typed by its kind, and every expression and ``{{ }}`` template compiled;
- the builder's own checks (the web's ``validate.ts``): one start step,
  connections that leave by one of a step's ways out, circles only through
  a loop, the settings each kind can't run without, and, as warnings, steps
  nothing reaches, branches that go nowhere and merges one way comes into.

What a draft's expressions read (``steps.<id>.output.<field>``) is checked
by the builder, where each step's output is typed, when the person opens it.
"""

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from forge_task_workflows.checks import problems as expression_problems
from forge_task_workflows.document import Workflow
from forge_task_workflows.errors import WorkflowFailed

from forge_admin.workflows import FORMAT, schema_problems

CATALOG_PATH = Path(__file__).with_name("workflow.catalog.json")
# What a draft is called until it's saved and the store gives it an ID.
DRAFT_ID = "wf_draft"
STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
ITEM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# An address: http(s) and no spaces except inside its {{ }} parts, or an
# expression in {{ }}; as the builder checks it (validate.ts).
URLISH = re.compile(
    r"^(https?://(?:\{\{[^}]*\}\}|[^\s{]|\{(?!\{))+|\{\{[\s\S]*\}\}\S*)$"
)
# A logic step's way out that runs when no branch does: never a slip.
FALLBACK = {"if": "false", "switch": "default", "match": "otherwise", "loop": "done"}


@lru_cache
def catalog() -> dict[str, Any]:
    """The step catalog: each kind's settings, defaults, ways out and output."""
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


# -- Completing a draft --------------------------------------------------------


def complete(draft: dict[str, Any], *, organization_id: str) -> dict[str, Any]:
    """
    A draft made a whole document: each step's settings over its kind's
    defaults, its ways out, IDs for a switch's cases, a match's rules and
    HTTP headers that have none, every connection's ID (a connection's
    ``output`` stands for ``source_output``), and the start step as its
    entry. What doesn't fit is kept as it is, for :func:`problems` to name.

    :param draft: The draft, as the assistant wrote it.
    :param organization_id: The organization it's for.
    :return: The document.
    """
    kinds = catalog()["kinds"]
    nodes: list[Any] = []
    for raw in _list(draft.get("nodes")):
        if not isinstance(raw, dict):
            nodes.append(raw)
            continue
        kind = raw.get("kind")
        spec = kinds.get(kind) if isinstance(kind, str) else None
        given = raw.get("config")
        config: dict[str, Any] = given if isinstance(given, dict) else {}
        if spec is not None:
            config = _over(spec["defaults"], config)
            for key, prefix in (
                ("cases", "case"),
                ("arms", "rule"),
                ("headers", "header"),
            ):
                if key in spec["defaults"]:
                    config[key] = _with_ids(config.get(key), prefix)
        name = raw.get("name")
        nodes.append(
            {
                "id": raw.get("id"),
                "kind": kind,
                "name": name
                if isinstance(name, str) and name.strip()
                else (spec or {}).get("label", ""),
                "config": config,
                "outputs": _outputs(spec, config)
                if spec
                else _list(raw.get("outputs")),
            }
        )
    edges: list[Any] = []
    for raw in _list(draft.get("edges")):
        if not isinstance(raw, dict):
            edges.append(raw)
            continue
        source, target = raw.get("source"), raw.get("target")
        output = raw.get("source_output", raw.get("output", "next"))
        edges.append(
            {
                "id": f"{source}:{output}->{target}",
                "source": source,
                "source_output": output,
                "target": target,
            }
        )
    entry = next(
        (n["id"] for n in nodes if isinstance(n, dict) and n.get("kind") == "entry"),
        None,
    )
    moment = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    name = draft.get("name")
    return {
        "format": FORMAT,
        "id": DRAFT_ID,
        "name": name.strip()
        if isinstance(name, str) and name.strip()
        else "Untitled workflow",
        "description": draft.get("description")
        if isinstance(draft.get("description"), str)
        else "",
        "organization_id": organization_id,
        "entry": entry,
        "nodes": nodes,
        "edges": edges,
        "layout": draft["layout"] if isinstance(draft.get("layout"), dict) else {},
        "created_at": moment,
        "updated_at": moment,
    }


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _over(defaults: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """The defaults with what's given over them (an object merged a level down)."""
    merged = dict(defaults)
    for key, value in given.items():
        base = defaults.get(key)
        merged[key] = (
            {**base, **value}
            if isinstance(base, dict) and isinstance(value, dict)
            else value
        )
    return merged


def _with_ids(items: Any, prefix: str) -> Any:
    """A list setting's items, each with an ID (``case_1``, ...) where it has none."""
    if not isinstance(items, list):
        return items
    taken = {item.get("id") for item in items if isinstance(item, dict)}
    count = 0
    filled = []
    for item in items:
        if isinstance(item, dict) and not (
            isinstance(item.get("id"), str) and item["id"]
        ):
            while f"{prefix}_{count + 1}" in taken:
                count += 1
            count += 1
            item = {**item, "id": f"{prefix}_{count}"}
            taken.add(item["id"])
        filled.append(item)
    return filled


def _outputs(spec: dict[str, Any], config: dict[str, Any]) -> list[Any]:
    """A step's ways out, in order: fixed by its kind, or one per case or rule."""
    outputs = spec["outputs"]
    if isinstance(outputs, list):
        return list(outputs)
    items = config.get("cases") if "cases" in config else config.get("arms")
    ids = [item.get("id") for item in _list(items) if isinstance(item, dict)]
    return [*ids, *outputs["then"]]


# -- Checking a draft ----------------------------------------------------------


def problems(document: dict[str, Any]) -> dict[str, list[str]]:
    """
    Everything that stops a completed draft from being saved or run
    (``errors``) or that probably isn't what was meant (``warnings``).

    :param document: A document :func:`complete` made.
    :return: ``{"errors", "warnings"}``, each a list of sentences naming
        where.
    """
    warnings: list[str] = []
    errors = schema_problems(document)
    graph_errors, sound = _graph(document, warnings)
    errors += graph_errors
    # The runner reads a document only once its steps and start are sound.
    if sound:
        try:
            workflow = Workflow.parse(document)
        except WorkflowFailed as failed:
            errors.append(str(failed))
        else:
            errors += expression_problems(workflow)
    return {"errors": _unique(errors), "warnings": _unique(warnings)}


def _unique(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(lines))


def _label(node: dict[str, Any]) -> str:
    return f"{node.get('name') or node.get('id')} ({node.get('id')})"


def _graph(document: dict[str, Any], warnings: list[str]) -> tuple[list[str], bool]:
    """
    The builder's checks of the graph and each step's settings.

    :return: ``(errors, sound)``: sound when every step has an ID and a kind
        of its own and there's one start step, so the runner can read it.
    """
    kinds = catalog()["kinds"]
    errors: list[str] = []
    nodes = [n for n in _list(document.get("nodes")) if isinstance(n, dict)]
    by_id: dict[str, dict[str, Any]] = {}
    sound = len(nodes) == len(_list(document.get("nodes")))
    for node in nodes:
        step_id, kind = node.get("id"), node.get("kind")
        if (
            not isinstance(step_id, str)
            or not STEP_ID.match(step_id)
            or kind not in kinds
        ):
            sound = False  # the schema check names it
            continue
        if step_id in by_id:
            errors.append(f"Two steps are {step_id}: every step's ID is its own.")
            sound = False
            continue
        by_id[step_id] = node
    entries = [n for n in by_id.values() if n["kind"] == "entry"]
    if not entries:
        errors.append(
            'Add a start step (kind "entry", ID "start"): every run starts there.'
        )
        sound = False
    elif len(entries) > 1:
        errors.append(
            f"A workflow has one start step; this has {len(entries)}: "
            + ", ".join(_label(n) for n in entries)
            + "."
        )
        sound = False

    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    for edge in _list(document.get("edges")):
        if not isinstance(edge, dict):
            continue
        source, target, output = (
            edge.get("source"),
            edge.get("target"),
            edge.get("source_output"),
        )
        if source not in by_id:
            errors.append(f"A connection leaves {source!r}, which isn't a step.")
            continue
        if target not in by_id:
            errors.append(
                f"A connection from {_label(by_id[source])} goes to {target!r}, which isn't a step."
            )
            continue
        outputs = _list(by_id[source].get("outputs"))
        if output not in outputs:
            errors.append(
                f"{_label(by_id[source])} has no way out {output!r}; its ways out are "
                + (", ".join(map(str, outputs)) or "none")
                + "."
            )
            continue
        outgoing[source].append((str(output), str(target)))
        incoming[str(target)].append(source)
    for entry in entries:
        if incoming.get(entry["id"]):
            errors.append("Nothing may lead back to the start step.")

    for component in _circles(list(by_id), outgoing):
        if not any(by_id[step_id]["kind"] == "loop" for step_id in component):
            names = " → ".join(_label(by_id[step_id]) for step_id in component)
            errors.append(
                f"{names} go round in a circle: only a loop may repeat steps."
            )

    reached: set[str] = set()
    queue = [entries[0]["id"]] if entries else []
    while queue:
        at = queue.pop()
        if at not in reached:
            reached.add(at)
            queue.extend(target for _, target in outgoing[at])
    for step_id, node in by_id.items():
        kind = node["kind"]
        if entries and step_id not in reached:
            warnings.append(f"{_label(node)}: no way from the start leads here.")
        if kind in FALLBACK:
            for output in _list(node.get("outputs")):
                if output == FALLBACK[kind] or any(
                    o == output for o, _ in outgoing[step_id]
                ):
                    continue
                warnings.append(
                    f"{_label(node)}'s body is empty: connect its each way to the steps "
                    "to repeat."
                    if kind == "loop"
                    else f"{_label(node)}'s {output} way goes nowhere: a run that takes it "
                    "ends there."
                )
        if kind == "merge" and len(incoming[step_id]) < 2:
            warnings.append(
                f"{_label(node)} is a merge, but {len(incoming[step_id])} way(s) come "
                "into it; it waits for two or more."
            )
        errors += _settings(node, warnings)
    return errors, sound


def _circles(
    ids: list[str], outgoing: dict[str, list[tuple[str, str]]]
) -> list[list[str]]:
    """The steps that go round in circles: Tarjan's strongly connected components."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def visit(at: str) -> None:
        index[at] = low[at] = len(index)
        stack.append(at)
        on_stack.add(at)
        for _, target in outgoing.get(at, []):
            if target not in index:
                visit(target)
                low[at] = min(low[at], low[target])
            elif target in on_stack:
                low[at] = min(low[at], index[target])
        if low[at] == index[at]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == at:
                    break
            circular = len(component) > 1 or any(
                t == at for _, t in outgoing.get(at, [])
            )
            if circular:
                found.append(component[::-1])

    for step_id in ids:
        if step_id not in index:
            visit(step_id)
    return found


def _settings(node: dict[str, Any], warnings: list[str]) -> list[str]:
    """What a step's own settings lack for it to run, as the builder says it."""
    given = node.get("config")
    config: dict[str, Any] = given if isinstance(given, dict) else {}
    label = _label(node)
    errors: list[str] = []

    def need(ok: bool, message: str) -> None:
        if not ok:
            errors.append(f"{label}: {message}")

    def text(key: str) -> str:
        value = config.get(key)
        return value.strip() if isinstance(value, str) else ""

    match node.get("kind"):
        case "agent":
            need(
                bool(text("instructions")), "tell the agent what to do (instructions)."
            )
            if config.get("output") == "json":
                if text("output_schema"):
                    try:
                        json.loads(config["output_schema"])
                    except (TypeError, ValueError):
                        need(
                            False,
                            "its output_schema isn't valid JSON: it's a JSON Schema, as text.",
                        )
                else:
                    warnings.append(
                        f"{label}: describe the JSON it returns (output_schema), so later "
                        "steps can rely on it."
                    )
        case "approval":
            if not text("message"):
                warnings.append(
                    f"{label}: tell the approver what they're deciding (message)."
                )
        case "http":
            url = text("url")
            need(bool(url), "give it a URL.")
            if url:
                need(
                    bool(URLISH.match(url)),
                    "its URL starts with https:// or is an expression in {{ }}.",
                )
            listed = config.get("headers")
            headers = listed if isinstance(listed, list) else []
            need(
                all(
                    isinstance(h, dict) and str(h.get("name") or "").strip()
                    for h in headers
                ),
                "every header needs a name.",
            )
            if config.get("method") == "GET" and text("body"):
                warnings.append(
                    f"{label}: a GET request sends no body; pick another method or clear it."
                )
        case "transform":
            need(
                bool(text("expression")),
                "write the JSONata expression that makes what it hands on.",
            )
        case "delay":
            amount = config.get("amount")
            need(
                isinstance(amount, int | float) and amount > 0,
                "wait longer than zero (amount).",
            )
        case "subworkflow":
            need(
                bool(text("workflow")),
                "pick the organization's workflow it runs (its wf_ ID).",
            )
        case "if":
            need(bool(text("condition")), "write the condition.")
        case "switch":
            need(bool(text("value")), "say which value it switches on.")
            cases = [c for c in _list(config.get("cases")) if isinstance(c, dict)]
            need(bool(cases), "add a case.")
            values = [str(c.get("value") or "").strip() for c in cases]
            need(all(values), "every case needs the value that takes it.")
            filled = [v for v in values if v]
            need(
                len(set(filled)) == len(filled),
                "two cases have the same value; only the first would be taken.",
            )
        case "match":
            arms = [a for a in _list(config.get("arms")) if isinstance(a, dict)]
            need(bool(arms), "add a rule.")
            need(
                all(str(a.get("condition") or "").strip() for a in arms),
                "every rule needs its condition.",
            )
        case "loop":
            need(bool(text("items")), "say which list it goes through (items).")
            need(
                bool(ITEM_NAME.match(str(config.get("item_name") or ""))),
                "the item's name is one word: letters, digits and _.",
            )
    return errors
