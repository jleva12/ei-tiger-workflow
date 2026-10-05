"""Errors of the documents task."""

from forge_tasks.errors import TaskError


class UnsupportedFormatError(TaskError):
    permanent = True


class ParseError(TaskError):
    permanent = True


class FileTooLargeError(TaskError):
    permanent = True


class StaleRunError(TaskError):
    """A newer ingest run for the same document took over; this one must stop."""

    permanent = True
