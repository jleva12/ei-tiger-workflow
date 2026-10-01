"""What's wrong with a workflow before it runs: every JSONata expression, and
every ``{{ expression }}`` part of a text setting, compiled. A run checks its
document first and stops with the whole list, rather than failing at the one
step it reaches; the web builder shows the same problems as the workflow is
edited (its validate.ts), so this is the check nobody can skip.

Settings read as JSONata are listed here by kind; keep them in step with the
executors (nodes/) and the builder's expression fields.
"""

from __future__ import annotations

from collections.abc import Iterator

from forge_task_workflows.document import Node, Workflow
from forge_task_workflows.expressions import TEMPLATE, compile_errors


def _expressions(node: Node) -> Iterator[tuple[str, str]]:
    """(what it is, the expression) of each expression setting of a step."""
    s = node.settings
    match node.kind:
        case "http":
            yield "Its body", s.body
        case "transform":
            yield "Its expression", s.expression
        case "if":
            yield "Its condition", s.condition
        case "switch":
            yield "What it switches on", s.value
        case "match":
            for index, arm in enumerate(s.arms):
                yield f"The condition of {arm.label or f'rule {index + 1}'}", arm.condition
        case "loop":
            yield "Its list", s.items
        case "end":
            yield "Its result", s.result


def _templates(node: Node) -> Iterator[tuple[str, str]]:
    """(what it is, the text) of each text setting with ``{{ }}`` parts."""
    s = node.settings
    match node.kind:
        case "agent":
            yield "Its instructions", s.instructions
        case "approval":
            yield "Its message", s.message
        case "http":
            yield "Its URL", s.url
            for header in s.headers:
                yield f"Its {header.name or 'unnamed'} header", header.value


def problems(workflow: Workflow) -> list[str]:
    """
    :return: One line per expression that doesn't compile, naming the step
        and the setting; empty when every one does.
    """
    found: list[str] = []
    for node in workflow.nodes:
        for what, expression in _expressions(node):
            error = compile_errors(expression)
            if error:
                found.append(f"{node.label}: {what.lower()} isn't valid JSONata: {error}")
        for what, text in _templates(node):
            for part in TEMPLATE.findall(text):
                error = compile_errors(part)
                if error:
                    found.append(
                        f"{node.label}: {what.lower()} has {{{{ {part.strip()} }}}}, which isn't valid JSONata: {error}"
                    )
    return found
