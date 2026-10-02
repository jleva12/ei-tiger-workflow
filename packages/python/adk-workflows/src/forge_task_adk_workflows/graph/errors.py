"""How building or running an agent goes wrong."""

from typing import Any


class AgentBuildError(Exception):
    """An agent ADK can't be built from; the message says why, naming the node."""


class RunFailed(Exception):
    """
    The run can't go on: input that doesn't fit its start, an End that fails
    the run, a step that failed with no way to take, a limit reached. Raised in
    a node, it ends ADK's run: ``Runner.run_async`` raises it.

    :ivar message: Why, naming the step.
    :ivar result: The run's result, for an End that fails it.
    :ivar step: The ID of the step it failed at, when it's one.
    """

    def __init__(self, message: str, *, result: Any = None, step: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.result = result
        self.step = step
