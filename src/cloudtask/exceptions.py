class CloudTaskException(Exception):
    """Base exception for all cloudtask errors."""

    pass


class InvalidTaskSecretError(CloudTaskException):
    """Raised when the request header secret does not match the client secret."""

    pass


class TaskNotFound(CloudTaskException):
    """Raised when the module or function path cannot be imported."""

    pass


class TaskExecutionError(CloudTaskException):
    """Raised when the task function fails during execution."""

    pass
