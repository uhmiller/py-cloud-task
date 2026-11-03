import inspect
import logging
from collections.abc import Callable, Mapping
from typing import Any

try:
    from asgiref.sync import async_to_sync
except ImportError:
    async_to_sync = None

from cloudtask import CloudTaskClient, Task, exceptions

logger = logging.getLogger("cloudtask.executor")


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

    async def run(self, body: dict[str, Any], headers: Mapping[str, str]) -> Any:
        """Async entrypoint (For FastAPI/ASGI)."""
        self.authenticator.authenticate(headers)
        func, args, kwargs = self._prepare_execution(body)
        return await self._execute_impl(func, args, kwargs)

    def sync_run(self, body: dict[str, Any], headers: Mapping[str, str]) -> Any:
        """Sync entrypoint (For Django WSGI/Flask)."""
        self.authenticator.authenticate(headers)
        func, args, kwargs = self._prepare_execution(body)
        return self._sync_execute_impl(func, args, kwargs)

    def _prepare_execution(self, body: dict[str, Any]):
        """Extract the task path and arguments from an authenticated request and resolve the task."""
        try:
            func_path: str = body["path"]
            func_args: list[Any] = body.get("args", [])
            func_kwargs: dict[str, Any] = body.get("kwargs", {})
        except KeyError as e:
            msg = f"Invalid payload: missing field {e!s}"
            logger.error(msg)
            raise exceptions.TaskExecutionError(msg)

        logger.info("Processing task: %s", func_path)

        func = self.resolve_function(func_path)
        return func, func_args, func_kwargs

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
        except Exception as e:
            logger.exception("Async task failed: %s", func.__name__)
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

        except Exception as e:
            logger.exception("Sync task failed: %s", func.__name__)
            raise exceptions.TaskExecutionError(str(e)) from e
