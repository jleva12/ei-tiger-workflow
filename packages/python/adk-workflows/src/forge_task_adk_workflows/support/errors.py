"""How a step goes wrong."""

from __future__ import annotations

from typing import Any


class StepFailed(Exception):
    """
    A step couldn't do its job (an HTTP 500, an expression that failed, an
    answer that doesn't fit its schema). The step takes its Error way when it
    has one connected; otherwise the run fails with it.

    :param message: What went wrong.
    :param status: The HTTP status it ended with, if any.
    :param details: More of what went wrong (a response's body, the expression).
    """

    def __init__(self, message: str, *, status: int | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}

    def as_error(self) -> dict[str, Any]:
        """:return: The error as ``steps.<id>.error`` reads it: ``{message, status?, ...details}``."""
        error: dict[str, Any] = {"message": self.message}
        if self.status is not None:
            error["status"] = self.status
        error.update(self.details)
        return error
