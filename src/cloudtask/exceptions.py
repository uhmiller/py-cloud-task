class CloudTaskException(Exception):
    """Base exception for all cloudtask errors."""


class ImproperlyConfigured(CloudTaskException):
    """Raised when the client configuration is unsafe or incomplete (e.g. a worker without authentication)."""


class TaskAuthenticationError(CloudTaskException):
    """Raised when an incoming task request fails authentication."""


class InvalidTaskSecretError(TaskAuthenticationError):
    """Raised when the request header secret does not match the client secret."""


class TaskNotFound(CloudTaskException):
    """Raised when the requested task path is not registered in the client."""


class TaskExecutionError(CloudTaskException):
    """Raised when the task function fails during execution."""


class TaskPushError(CloudTaskException):
    """Raised when an error occurs while pushing task to queue."""


class TaskAlreadyExists(TaskPushError):
    """Raised by ``push(if_exists="raise")`` when a task with the same name already exists (or existed recently)."""


class InvalidTaskPayload(CloudTaskException):
    """Raised when an incoming task request body is malformed or doesn't match the task's arguments."""


class TaskAbort(CloudTaskException):
    """
    Raise inside a task to stop it for good: the worker answers 2xx, so Cloud Tasks does not retry.

    Use it for permanent conditions (the order is already paid, the entity was deleted). The reason is logged and
    returned in the response: keep personal data out of it.
    """

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


class TaskRetry(CloudTaskException):
    """
    Raise inside a task to ask for a retry without logging it as an error. The worker answers 503.

    The retry delay is the queue's backoff (Cloud Tasks ignores ``Retry-After``).
    """
