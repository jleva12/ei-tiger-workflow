"""Error taxonomy shared by every task type. ``permanent`` decides whether the
worker retries: a :class:`TransientError` is retried with backoff, anything
else fails the job."""


class TaskError(Exception):
    """Base error. Permanent unless it's a TransientError (retrying won't help)."""

    permanent: bool = True


class TransientError(TaskError):
    """Network / rate-limit / storage hiccups. Safe to retry."""

    permanent = False


class StorageError(TransientError):
    pass


def from_pymongo(exc: Exception) -> TaskError:
    """Classify a driver error: network/failover/retryable-labelled errors are
    transient (retry the job); everything else (bad query, document too large,
    auth) is permanent."""
    from pymongo.errors import ConnectionFailure, OperationFailure, PyMongoError

    if isinstance(exc, ConnectionFailure):  # AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError, ...
        return StorageError(f"mongo (transient): {exc}")
    if isinstance(exc, PyMongoError) and (
        exc.has_error_label("RetryableWriteError") or exc.has_error_label("TransientTransactionError")
    ):
        return StorageError(f"mongo (transient): {exc}")
    if isinstance(exc, OperationFailure) and exc.code in (91, 189, 6, 7, 89, 9001, 10107, 11600, 11602, 13435, 13436):
        return StorageError(f"mongo (transient, code {exc.code}): {exc}")  # shutdown/stepdown/network codes
    err = TaskError(f"mongo: {exc}")
    err.permanent = True
    return err
