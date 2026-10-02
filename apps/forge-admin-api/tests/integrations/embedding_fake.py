"""A fake of the async worker's queues, standing in for
``forge_admin.embedding.Embedding``."""

from forge_admin.embedding import EmbeddingError


class FakeEmbedding:
    """Takes the jobs that take ADK workflow runs, as the async worker's queue
    does, and records which run each names."""

    def __init__(self) -> None:
        # The runs queued, in order: one entry per job.
        self.queued: list[str] = []
        # Set to make every call fail as an unreachable Redis does.
        self.refuse = False

    async def run_adk(self, run_id: str) -> None:
        if self.refuse:
            raise EmbeddingError("Error 111 connecting to redis:6379")
        self.queued.append(run_id)

    async def aclose(self) -> None:
        pass
