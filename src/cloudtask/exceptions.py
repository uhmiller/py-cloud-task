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
