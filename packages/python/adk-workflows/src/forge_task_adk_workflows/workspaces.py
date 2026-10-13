"""
A run's file system: a folder of its own on the worker's mounted volume
(``AdkWorkflowsSettings.workspaces``), for a workflow whose start allows a
file system (``allow_file_system``):

    <workspaces>/<organization>/<workflow>/<run>

It's made as the run goes on, and is the same folder for every attempt of
the run (its pauses, retries and starting over), since the run's ID stays the
same; two runs, even of one workflow, never share one. A saved workflow it
runs whose start allows one works in the run's folder too. Its start keeps the
folder's path in the session's state as ``workspace``, where its steps and
its LLM agents' instructions read it (``{{ state.workspace }}``): e.g. to
clone a repository into ``{{ state.workspace }}/backend``.

Each part of the path is the ID as it is when it's a plain folder name
(letters, digits, ``.``, ``_``, ``-``), else a hash of it, so no ID reaches
outside the volume. Nothing here removes a run's folder: what's in it stays
after the run ends.
"""

import hashlib
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from forge_task_adk_workflows.graph.factories.base import config_of, items
from forge_task_adk_workflows.graph.uses import saved_in

#: The longest part of a folder's path kept as it is.
MAX_PART = 128
_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class WorkspaceError(Exception):
    """The run's folder can't be made: the message says why."""


def allows_file_system(document: Mapping[str, Any]) -> bool:
    """:return: Whether a workflow's start allows a file system (only a true does)."""
    for node in items(document.get("nodes")):
        if isinstance(node, dict) and node.get("kind") == "start":
            return config_of(node).get("allow_file_system") is True
    return False


def documents_of(document: Mapping[str, Any], saved: Mapping[str, Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """
    :param document: The workflow a run runs.
    :param saved: The saved workflows it runs, by reference (``RunPayload.saved``).
    :return: It, and the saved workflows it runs (however deep), each once.
    """
    seen: set[str] = set()
    waiting: list[Mapping[str, Any]] = [document]
    while waiting:
        each = waiting.pop()
        yield each
        for ref in saved_in(dict(each)):
            found = saved.get(ref)
            if found is not None and ref not in seen:
                seen.add(ref)
                waiting.append(found)


def uses_file_system(document: Mapping[str, Any], saved: Mapping[str, Mapping[str, Any]]) -> bool:
    """:return: Whether a run's start, or a saved workflow's it runs (however
    deep), allows a file system (:func:`documents_of`)."""
    return any(allows_file_system(each) for each in documents_of(document, saved))


def runs_commands(document: Mapping[str, Any], saved: Mapping[str, Mapping[str, Any]]) -> bool:
    """:return: Whether a run's workflow, or a saved workflow it runs (however
    deep), has a Bash step (:func:`documents_of`)."""
    return any(
        isinstance(node, dict) and node.get("kind") == "bash"
        for each in documents_of(document, saved)
        for node in items(each.get("nodes"))
    )


def folder_name(value: str) -> str:
    """:return: ``value`` as one part of a folder's path: as it is when it's a
    plain name, else ``id-`` and a hash of it."""
    if _PLAIN.fullmatch(value) and len(value) <= MAX_PART and value not in {".", ".."}:
        return value
    return "id-" + hashlib.sha256(value.encode()).hexdigest()[:32]


def workspace_of(root: str | Path, *, organization: str, workflow: str, run: str) -> Path:
    """:return: Where a run's folder is under the volume ``root`` (made or not)."""
    base = Path(root).expanduser().absolute()
    return base / folder_name(organization) / folder_name(workflow) / folder_name(run)


def make_workspace(root: str | Path, *, organization: str, workflow: str, run: str) -> Path:
    """
    Make a run's folder (and the volume's folders on the way to it), unless
    it's there: only the worker's user may use what it makes.

    :return: The folder.
    :raises WorkspaceError: When it can't be made, or would be outside the
        volume (a link on the way leading elsewhere).
    """
    folder = workspace_of(root, organization=organization, workflow=workflow, run=run)
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        inside = folder.resolve().is_relative_to(Path(root).expanduser().resolve())
    except OSError as error:
        raise WorkspaceError(f"its folder ({folder}) can't be made: {error.strerror or error}") from None
    if not inside:
        raise WorkspaceError(f"its folder ({folder}) leads outside {root}")
    return folder
