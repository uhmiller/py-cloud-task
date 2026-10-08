class CloudTaskException(Exception):
    """Base exception for all cloudtask errors."""


class InvalidTaskSecretError(CloudTaskException):
    """Raised when the request header secret does not match the client secret."""


class TaskNotFound(CloudTaskException):
    """Raised when the module or function path cannot be imported."""


class TaskExecutionError(CloudTaskException):
    """Raised when the task function fails during execution."""


class TaskPushError(CloudTaskException):
    """Raised when an error occurs while pushing task to queue."""
