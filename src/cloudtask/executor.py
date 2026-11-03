import inspect
import logging
from collections.abc import Callable, Mapping
from typing import Any

try:
    from asgiref.sync import async_to_sync
except ImportError:
    async_to_sync = None

from cloudtask import CloudTaskClient, Task, exceptions
from cloudtask.auth import normalize_headers
from cloudtask.context import TaskContext, task_context_scope
from cloudtask.core import task_path_of

logger = logging.getLogger("cloudtask.executor")

# Raised on purpose by tasks or by payload validation: never wrapped into TaskExecutionError.
CONTROL_FLOW_EXCEPTIONS = (
    exceptions.TaskAbort,
    exceptions.TaskRetry,
    exceptions.InvalidTaskPayload,
)


def _log_control_flow(path: str, error: Exception) -> None:
    if isinstance(error, exceptions.TaskAbort):
        logger.warning("Task %s aborted: %s", path, error.reason)
    elif isinstance(error, exceptions.TaskRetry):
        logger.info("Task %s asked for a retry", path)
    else:
        logger.error("Task %s rejected: %s", path, error)


class CloudTaskExecutor:
    """
    Framework-agnostic processor for incoming Cloud Tasks.
    Provides both Sync and Async execution methods.

    Every request must carry the client's shared secret, and only tasks registered in the client
    (``@client.task``) can run.
    """

    def __init__(self, client: CloudTaskClient):
        """
        Raises:
            ImproperlyConfigured: The client has no worker authentication configured.
        """
        self.client = client
        self.authenticator = client.authenticator

    async def run(self, body: Any, headers: Mapping[str, str]) -> Any:
        """Async entrypoint (For FastAPI/ASGI)."""
        self.authenticator.authenticate(headers)
        func, args, kwargs = self._prepare_execution(body)
        with task_context_scope(self._task_context(func, headers)):
            return await self._execute_impl(func, args, kwargs)

    def sync_run(self, body: Any, headers: Mapping[str, str]) -> Any:
        """Sync entrypoint (For Django WSGI/Flask)."""
        self.authenticator.authenticate(headers)
        func, args, kwargs = self._prepare_execution(body)
        with task_context_scope(self._task_context(func, headers)):
            return self._sync_execute_impl(func, args, kwargs)

    def _task_context(self, func: Callable, headers: Mapping[str, str]) -> TaskContext:
        return TaskContext.from_headers(
            task_path_of(func),
            normalize_headers(headers),
            hidden=frozenset({self.client.secret_header_name.lower()}),
        )

    def _prepare_execution(self, body: Any):
        """
        Extract the task path and arguments from an authenticated request and resolve the task.

        Raises:
            InvalidTaskPayload: The body is malformed or its arguments don't fit the task's signature.
            TaskNotFound: The path is not a registered task.
        """
        if not isinstance(body, dict):
            raise self._invalid("body must be a JSON object")
        func_path = body.get("path")
        func_args = body.get("args", [])
        func_kwargs = body.get("kwargs", {})
        if not isinstance(func_path, str) or not func_path:
            raise self._invalid("missing field 'path'")
        if not isinstance(func_args, list):
            raise self._invalid("'args' must be a list")
        if not isinstance(func_kwargs, dict):
            raise self._invalid("'kwargs' must be an object")

        logger.info("Processing task: %s", func_path)

        func = self.resolve_function(func_path)
        try:
            # Partial: FastAPI dependencies are filled in later.
            inspect.signature(func).bind_partial(*func_args, **func_kwargs)
        except TypeError as e:
            raise self._invalid(
                f"arguments don't match task '{func_path}': {e}"
            ) from None

        validator = getattr(func, "__cloudtask_validator__", None)
        if validator is not None:
            try:
                func_args, func_kwargs = validator.validate(func_args, func_kwargs)
            except exceptions.InvalidTaskPayload as e:
                logger.error("Invalid task payload for %s: %s", func_path, e)
                raise
        return func, func_args, func_kwargs

    @staticmethod
    def _invalid(reason: str) -> exceptions.InvalidTaskPayload:
        logger.error("Invalid task payload: %s", reason)
        return exceptions.InvalidTaskPayload(f"Invalid payload: {reason}")

    def resolve_function(self, path: str) -> Callable[..., Task]:
        """
        Return the registered task factory for ``path``. Never imports anything from request data.

        Raises:
            TaskNotFound: The path is not a registered task.
        """
        try:
            return self.client.get_task(path)
        except exceptions.TaskNotFound:
            logger.error("Task resolution failed: %s is not a registered task", path)
            raise

    @staticmethod
    async def _call(func: Callable, args: list, kwargs: dict) -> Any:
        """Build the task and run it, awaiting async tasks. Exceptions propagate unchanged."""
        task: Task = func(*args, **kwargs)
        result = task.execute()
        if inspect.isawaitable(result):
            return await result
        return result

    async def _execute_impl(self, func: Callable, args: list, kwargs: dict) -> Any:
        """Internal execution logic for Async Context."""
        try:
            return await self._call(func, args, kwargs)
        except CONTROL_FLOW_EXCEPTIONS as e:
            _log_control_flow(task_path_of(func), e)
            raise
        except Exception as e:
            logger.exception("Async task failed: %s", task_path_of(func))
            raise exceptions.TaskExecutionError(str(e)) from e

    @staticmethod
    def _sync_execute_impl(func: Callable, args: list, kwargs: dict) -> Any:
        """Internal execution logic for Sync Context."""
        try:
            task: Task = func(*args, **kwargs)

            # try with asgiref
            if task.is_coroutine:
                if async_to_sync:

                    async def _runner():
                        return await task.execute()

                    return async_to_sync(_runner)()
                raise exceptions.TaskExecutionError(
                    "Cannot execute async task in sync context without 'asgiref' installed."
                )
            return task.execute()

        except exceptions.TaskExecutionError:
            raise
        except CONTROL_FLOW_EXCEPTIONS as e:
            _log_control_flow(task_path_of(func), e)
            raise
        except Exception as e:
            logger.exception("Sync task failed: %s", task_path_of(func))
            raise exceptions.TaskExecutionError(str(e)) from e
