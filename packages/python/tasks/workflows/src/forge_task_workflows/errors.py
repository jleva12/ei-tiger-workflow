"""How a workflow run goes wrong.

- :class:`StepFailed`: a step couldn't do its job (an HTTP 500, an operation
  the provider refused, a child workflow that failed). The step takes its error
  way when it has one connected; otherwise the run fails with it.
- :class:`WorkflowFailed`: the run can't go on (a document that isn't a
  workflow, a limit reached, a step failing with no way to take). Permanent:
  retrying won't help.
- Anything a service raises as a ``TransientError`` (a network hiccup, a rate
  limit) fails the attempt retryably: the worker retries the run, which picks
  up after the last step it finished.
"""

from __future__ import annotations

from typing import Any

from forge_tasks.errors import TaskError


class StepFailed(Exception):
    """
    Represents an exception that occurs due to a step failure.

    Used to capture and communicate details about a specific failure, allowing
    later steps or processes to analyze and respond to the error appropriately.

    :ivar message: The error message describing the failure.
    :type message: str
    :ivar status: Optional status code associated with the failure.
    :type status: int | None
    :ivar details: Additional details about the failure, provided as a dictionary.
    :type details: dict[str, Any]
    """

    def __init__(self, message: str, *, status: int | None = None, details: dict[str, Any] | None = None) -> None:
        """
        Initializes an instance of the class with a message, optional status code, and additional
        details. This class is designed to capture an error message, an optional HTTP status code,
        and a dictionary of supplementary detail information related to the error context.

        :param message: The main error message that explains the issue.
        :type message: str
        :param status: Optional HTTP status code associated with the error. If not provided,
                       defaults to None.
        :type status: int | None
        :param details: A dictionary containing additional information or context regarding the
                        error. Defaults to an empty dictionary if not provided.
        :type details: dict[str, Any] | None
        """
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}

    def as_error(self) -> dict[str, Any]:
        """
        Converts the instance data into a dictionary representing an error.

        This method builds an error representation from the instance's attributes and
        returns it as a dictionary. The "message" attribute is always included in
        the resulting dictionary. If the "status" attribute is not None, it is added
        to the dictionary. Additionally, all key-value pairs from the "details"
        attribute are also included.

        :return: A dictionary containing the error representation of the instance.
        :rtype: dict[str, Any]
        """
        error: dict[str, Any] = {"message": self.message}
        if self.status is not None:
            error["status"] = self.status
        error.update(self.details)
        return error


class WorkflowFailed(TaskError):
    permanent = True


class NotSetUp(StepFailed):
    """
    Represents an exception being raised when a required setup step has not been completed.

    This exception indicates that some prerequisite configuration or initialization is
    missing, which may prevent the execution of further steps in a process. The class
    inherits from `StepFailed` as part of a broader exception hierarchy for a
    step-based process.

    :ivar message: The error message associated with this exception.
    :type message: str
    :ivar code: The error code representing the specific failure scenario.
    :type code: int
    """
