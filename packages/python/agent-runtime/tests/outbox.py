"""The organization's workflows as the hosted runtime runs them, for tests:
runs an intake or a request starts are written down first (the outbox),
then started once each, however often they're asked for."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from forge_agent_runtime.services import StartSource, WorkflowStart, workflow_starter


class Outbox:
    """
    Workflows run through an outbox: :meth:`intend` writes each run down
    under an ID made from its call ID (the record's or request's), and
    :meth:`dispatch` starts it, once.

    :param refuse: Why every start is refused (its input doesn't fit, say).
    :param result: What each run comes back with, by how many started before it.
    """

    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        #: The runs written down, by run ID.
        self.intents: dict[str, dict[str, Any]] = {}
        #: Every run started, in order, each once: (workflow, input, call ID, wait).
        self.runs: list[tuple[str, dict[str, Any], str, float]] = []
        #: Every dispatch asked for, repeats too.
        self.dispatched: list[str] = []
        #: What said it was starting each run written down (workflow_starter).
        self.starters: list[str | None] = []

    def result(self, number: int) -> dict[str, Any]:
        return {"number": number}

    @staticmethod
    def run_id(agent_id: str, call_id: str) -> str:
        return hashlib.sha256(f"{agent_id}\n{call_id}".encode()).hexdigest()[:32]

    async def intend(
        self,
        starts: Sequence[WorkflowStart],
        *,
        organization_id: str | None,
        context: Any,
        source: StartSource,
    ) -> list[str]:
        ids = []
        for start in starts:
            run_id = self.run_id(context.session.app_name, start.call_id)
            if run_id not in self.intents:
                self.intents[run_id] = {"start": start, "source": source, "status": "intended"}
                self.starters.append(workflow_starter())
            ids.append(run_id)
        return ids

    async def dispatch(self, run_id: str, *, organization_id: str | None, wait: float) -> dict[str, Any]:
        self.dispatched.append(run_id)
        intent = self.intents[run_id]
        if self.refuse is not None:
            intent["status"] = "dropped"
            return {"run_id": run_id, "status": "refused", "error": self.refuse}
        if intent["status"] != "dispatched":
            intent["status"] = "dispatched"
            start: WorkflowStart = intent["start"]
            self.runs.append((start.ref, start.input, start.call_id, wait))
            intent["number"] = len(self.runs)
        return {"run_id": run_id, "status": "succeeded", "result": self.result(intent["number"])}

    async def run(self, workflow_id, workflow_input, *, organization_id, context, wait=None):
        raise AssertionError("Intakes start their workflows through the outbox.")

    async def progress(self, **_: Any) -> dict[str, Any] | None:
        return None
