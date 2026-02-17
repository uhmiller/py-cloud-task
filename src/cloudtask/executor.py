import importlib
import inspect
import logging
from typing import Any, Callable, Dict, List

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
    """

    def __init__(self, client: CloudTaskClient):
        self.client = client

    async def run(self, body: Dict[str, Any], headers: Dict[str, str]) -> Any:
        """Async entrypoint (For FastAPI/ASGI)."""
        func, args, kwargs = self._prepare_execution(body, headers)
        return await self._execute_impl(func, args, kwargs)

    def sync_run(self, body: Dict[str, Any], headers: Dict[str, str]) -> Any:
        """Sync entrypoint (For Django WSGI/Flask)."""
        func, args, kwargs = self._prepare_execution(body, headers)
        return self._sync_execute_impl(func, args, kwargs)

    def _prepare_execution(self, body: Dict[str, Any], headers: Dict[str, str]):
        """Shared validation and extraction logic."""

        if self.client.secret:
            self._validate_secret(headers)

        try:
            func_path: str = body["path"]
            func_args: List[Any] = body.get("args", [])
            func_kwargs: Dict[str, Any] = body.get("kwargs", {})
        except KeyError as e:
            msg = f"Invalid payload: missing field {str(e)}"
            logger.error(msg)
            raise exceptions.TaskExecutionError(msg)

        logger.info(f"Processing task: {func_path}")

        func = self.resolve_function(func_path)
        return func, func_args, func_kwargs

    def _validate_secret(self, headers: Dict[str, str]):
        """Robust secret validation handling generic HTTP headers."""
        expected_header = self.client.secret_header_name.lower()

        normalized_headers = {}
        for k, v in headers.items():
            key = k.lower().replace("_", "-")
            if key.startswith("http-"):
                key = key[5:]
            normalized_headers[key] = v

        received_secret = normalized_headers.get(expected_header)

        if received_secret != self.client.secret:
            logger.warning(
                f"Security violation: Invalid secret. Expected '{self.client.secret_header_name}'"
            )
            raise exceptions.InvalidTaskSecretError("Invalid or missing task secret.")

    @staticmethod
    async def _execute_impl(func: Callable, args: list, kwargs: dict) -> Any:
        """Internal execution logic for Async Context."""
        try:
            task: Task = func(*args, **kwargs)
            result = task.execute()

            if inspect.isawaitable(result):
                return await result

            return result

        except Exception as e:
            logger.exception(f"Async Task Failed '{func.__name__}': Error: {str(e)}")
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
            logger.exception(f"Sync Task Failed '{func.__name__}': Error: {str(e)}")
            raise exceptions.TaskExecutionError(str(e)) from e

    @staticmethod
    def resolve_function(path: str) -> Callable:
        try:
            if "." not in path:
                raise ValueError("Path must be in 'module.function' format")

            module_name, func_name = path.rsplit(".", 1)
            module = importlib.import_module(module_name)
            func = getattr(module, func_name)
            return func

        except (ImportError, AttributeError, ValueError) as e:
            logger.error(f"Task resolution failed: {path} - {str(e)}")
            raise exceptions.TaskNotFound(f"Could not import task '{path}': {str(e)}")
