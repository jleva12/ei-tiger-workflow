"""A fake of the async worker's queues, standing in for
``forge_admin.embedding.Embedding``."""

from typing import Any

from forge_admin.embedding import EmbeddingError


class FakeEmbedding:
    """Takes workflow runs and ADK workflow runs as the async worker's queues
    do and records them: one per key, as SAQ keeps them."""

    def __init__(self) -> None:
        # Workflow runs, as submitted.
        self.runs: list[dict[str, Any]] = []
        # ADK workflow runs, as submitted to their own queue.
        self.adk_runs: list[dict[str, Any]] = []
        # Set to make every call fail as an unreachable Redis does.
        self.refuse = False

    async def run_workflow(self, **submission: Any) -> None:
        self._check()
        if any(run["key"] == submission["key"] for run in self.runs):
            return
        self.runs.append(submission)

    async def run_adk_workflow(self, **submission: Any) -> None:
        self._check()
        if any(run["key"] == submission["key"] for run in self.adk_runs):
            return
        self.adk_runs.append(submission)

    async def aclose(self) -> None:
        pass

    def _check(self) -> None:
        if self.refuse:
            raise EmbeddingError("Error 111 connecting to redis:6379")
