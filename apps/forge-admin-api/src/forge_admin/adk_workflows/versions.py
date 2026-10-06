"""
Which version of a workflow runs, by reference: ``ag_x`` (its latest
published), ``ag_x@3``, or ``ag_x@draft``.

Two sets of rules, for two kinds of caller:

- **Inside**: a ``saved`` node, a workflow tool, the console's Run. A
  reference without a version means the latest published one, or, while the
  workflow has never been published, its current document: workflows saved
  before versions existed keep running as they did.
- **Outside** (the runtime): ``ag_x`` needs a published version; its draft is
  asked for by name, ``ag_x@draft``.
"""

from dataclasses import dataclass
from typing import Any, Literal

from forge_agent_runtime.refs import AgentNotFound, AgentRef

from forge_admin.adk_workflows.documents import AgentStore
from forge_admin.adk_workflows.runs import FindAgent, snapshot

WorkflowVersion = int | Literal["draft"]


class WorkflowNotFound(LookupError):
    """No such workflow, or version of one: the message says which."""


class WorkflowGone(WorkflowNotFound):
    """No such workflow at all."""


@dataclass(frozen=True)
class ResolvedWorkflow:
    """
    A workflow at a version.

    :ivar record: Its record (``AgentStore``).
    :ivar version: Which version: a published one's number, or ``"draft"``.
    :ivar document: That version's document, a copy; a published one carries
        its ``version``.
    """

    record: dict[str, Any]
    version: WorkflowVersion
    document: dict[str, Any]

    @property
    def run_record(self) -> dict[str, Any]:
        """:return: Its record, as a run of this version starts from it."""
        return {**self.record, "document": self.document}

    @property
    def ref(self) -> str:
        """:return: How it's named at this version: ``ag_x@3``, ``ag_x@draft``."""
        return f"{self.record['_id']}@{self.version}"


async def resolve_workflow(
    store: AgentStore,
    ref: str,
    *,
    organization_id: str | None,
    outside: bool = False,
) -> ResolvedWorkflow:
    """
    Find a workflow at the version a reference names.

    :param store: The workflows.
    :param ref: ``ag_x``, ``ag_x@3`` or ``ag_x@draft``.
    :param organization_id: The organization it must be; None to find it by
        ID alone (the runtime, which then checks who may call it).
    :param outside: Whether the caller is outside (the runtime): ``ag_x``
        then needs a published version.
    :raises WorkflowGone: There's no such workflow.
    :raises WorkflowNotFound: It has no such version, or (outside) none published.
    :raises PyMongoError: The store isn't answering.
    """
    try:
        parsed = AgentRef.parse(ref)
    except AgentNotFound as error:
        raise WorkflowNotFound(str(error)) from None
    workflow_id = parsed.agent_id
    record = (
        await store.get(organization_id, workflow_id)
        if organization_id is not None
        else await store.find(workflow_id)
    )
    if record is None:
        raise WorkflowGone(f"There's no workflow {workflow_id}.")
    if parsed.version == "draft":
        if not record.get("has_draft"):
            raise WorkflowNotFound(
                f"Workflow {workflow_id} has no draft: run one of its versions."
            )
        return ResolvedWorkflow(record, "draft", snapshot(record["document"]))
    number = (
        parsed.version
        if isinstance(parsed.version, int)
        else record.get("published_version")
    )
    if number is None:
        if outside:
            raise WorkflowNotFound(
                f"Workflow {workflow_id} hasn't been published: publish it, or "
                f"run its draft as {workflow_id}@draft."
            )
        return ResolvedWorkflow(record, "draft", snapshot(record["document"]))
    found = await store.version(record["organization_id"], workflow_id, int(number))
    if found is None:
        raise WorkflowNotFound(f"Workflow {workflow_id} has no version {number}.")
    return ResolvedWorkflow(record, int(number), snapshot(found["document"]))


def workflow_finder(store: AgentStore, organization_id: str) -> FindAgent:
    """
    :return: How a run finds the workflows it runs (``saved`` nodes, workflow
        tools) by reference, with the inside rules: their documents, or None
        for one that isn't there at all (the build names the node using it).
        It raises ``WorkflowNotFound`` for a version that isn't.
    """

    async def find(ref: str) -> dict[str, Any] | None:
        try:
            resolved = await resolve_workflow(
                store, ref, organization_id=organization_id
            )
        except WorkflowGone:
            return None
        return resolved.document

    return find
