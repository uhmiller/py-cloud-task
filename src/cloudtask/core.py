import inspect
import json
from datetime import datetime
from functools import wraps
from logging import getLogger
from typing import Any, Callable, Coroutine, Generic, Literal, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from google.cloud import tasks_v2
from google.protobuf import duration_pb2, timestamp_pb2

from cloudtask import exceptions
from cloudtask.encoder import CloudTaskJSONEncoder

# Type aliases for better readability in decorators
P = ParamSpec("P")
R = TypeVar("R")

logger = getLogger("cloudtask")


class Task(Generic[P, R]):
    """
    Represents a task wrapper that captures the function and its arguments,
    ready to be executed locally or pushed to Google Cloud Tasks.
    """

    def __init__(
        self,
        client: "CloudTaskClient",
        func: Callable[P, R],
        func_args: tuple,
        func_kwargs: dict,
        queue: str,
        url: str,
        name: str | None = None,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
    ):
        """
        Initializes the Task instance.

        Args:
            client (CloudTaskClient): The client instance responsible for configuration.
            func (Callable): The original function to be executed.
            func_args (tuple): Positional arguments for the function.
            func_kwargs (dict): Keyword arguments for the function.
            queue (str): The target Cloud Tasks queue name.
            url (str): The target URL (worker endpoint) that will process the task.
            name (str | None, optional): Explicit task name (deduplication ID). Defaults to None.
            timeout (int | None, optional): Dispatch deadline in seconds. Defaults to None.
            headers (dict | None, optional): Custom HTTP headers for the request. Defaults to None.
        """
        self.queue = queue
        self.url = url
        self.name = name
        self.timeout = timeout

        self._client = client
        self._headers = headers or {}

        self._func = func
        self._func_path = f"{func.__module__}.{func.__name__}"
        self._func_args = func_args
        self._func_kwargs = func_kwargs

        self._is_coroutine = inspect.iscoroutinefunction(func)

    def __call__(self) -> R | Coroutine[Any, Any, R]:
        """Allows the task instance to be called directly like the original function."""
        return self.execute()

    def execute(self) -> R | Coroutine[Any, Any, R]:
        """
        Executes the task immediately in the current process.

        This is used for:
        1. Local development (Eager mode).
        2. The actual execution logic inside the Worker/Consumer.

        Returns:
            R: The return value of the original function.
        """
        logger.debug(
            f"Executing task '{self._func.__name__}' immediately (local/worker)."
        )
        return self._func(*self._func_args, **self._func_kwargs)

    async def push(self, at: datetime | None = None) -> str | None:
        """
        Async push to Cloud Tasks. (Non-blocking IO)
        Recommended for FastAPI and Async Contexts.
        """
        # Eager Mode (Local Dev)
        if self._client.eager:
            if self._client.eager == "immediate":
                logger.info(f"[Eager-Async] Running '{self._func.__name__}' locally.")
                if self._is_coroutine:
                    await self.execute()
                else:
                    self.execute()
                return None
            elif self._client.eager == "remote":
                await self._remote()
                return None

        payload = self._build_task_request_payload(schedule_time=at)

        # Send via Async Client (gRPC aio)
        try:
            response = await self._client.service.create_task(request=payload)
            logger.info(f"Task created (Async): {response.name}")
            return response.name
        except Exception as e:
            self._handle_error(e)

    delay = push

    def sync_push(self, schedule_time: datetime | None = None) -> str | None:
        """
        Synchronous blocking push to Cloud Tasks.
        Use this ONLY in synchronous contexts (e.g., legacy Django views).
        """

        if self._client.eager:
            if self._client.eager == "immediate":
                logger.info(f"[Eager-Sync] Running '{self._func.__name__}' locally.")
                if self._is_coroutine:
                    logger.warning(
                        f"Task '{self._func.__name__}' is async but called via push_sync in eager mode. "
                        "You might need to await the result or use task.push() instead."
                    )
                self.execute()  # type: ignore
                return None
            elif self._client.eager == "remote":
                self._remote_sync()
                return None

        payload = self._build_task_request_payload(schedule_time)

        try:
            response = self._client.sync_service.create_task(request=payload)
            logger.info(f"Task created (Sync): {response.name}")
            return response.name
        except Exception as e:
            self._handle_error(e)

    sync_delay = sync_push

    async def _remote(self, url: str | None = None) -> Any:
        """
        Simulates the Google Cloud Task execution by making a direct HTTP POST request
        to the worker URL using 'httpx'.

        This bypasses Google Cloud infrastructure but tests the full HTTP/Serialization flow.

        Args:
            url (str | None): Override the task URL (useful if testing locally on a different port).

        Returns:
            Any: The JSON response from the worker.

        Raises:
            ImportError: If 'httpx' is not installed.
            Exception: If the worker returns a non-200 status.
        """
        try:
            import httpx
        except ImportError:
            raise ImportError(
                "The 'httpx' library is required for local remote simulation. "
                "Install it with: uv add httpx"
            )

        target_url = url or self.url
        payload = self.data
        headers = self.headers

        logger.info(f"⚡ Remote task: {self._func.__name__} -> {target_url}")

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    target_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout or 10.0,
                )

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Remote task failed with status {e.response.status_code}: {e.response.text}"
                )
                raise e
            except Exception as e:
                logger.error(f"Failed to connect to worker: {str(e)}")
                raise e

    def _remote_sync(self, url: str | None = None) -> Any:
        """
        Simulates the Google Cloud Task execution by making a direct HTTP POST request
        to the worker URL using 'httpx'.

        This bypasses Google Cloud infrastructure but tests the full HTTP/Serialization flow.

        Args:
            url (str | None): Override the task URL (useful if testing locally on a different port).

        Returns:
            Any: The JSON response from the worker.

        Raises:
            ImportError: If 'httpx' is not installed.
            Exception: If the worker returns a non-200 status.
        """
        try:
            import httpx
        except ImportError:
            raise ImportError(
                "The 'httpx' library is required for local remote simulation. "
                "Install it with: uv add httpx"
            )

        target_url = url or self.url
        payload = self.data
        headers = self.headers

        logger.info(f"⚡ Remote task: {self._func.__name__} -> {target_url}")

        with httpx.Client() as client:
            try:
                response = client.post(
                    target_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout or 10.0,
                )

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Remote task failed with status {e.response.status_code}: {e.response.text}"
                )
                raise e
            except Exception as e:
                logger.error(f"Failed to connect to worker: {str(e)}")
                raise e

    def _build_task_request_payload(self, schedule_time: datetime | None) -> dict:
        """Constructs the dictionary payload expected by Google Cloud Tasks API."""

        # Encoding payload to JSON bytes
        json_body = json.dumps(self.data, cls=CloudTaskJSONEncoder).encode("utf-8")

        http_request = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": self.url,
            "headers": self.headers,
            "body": json_body,
        }

        # OIDC Authentication configuration
        if self._client.sae:
            http_request["oidc_token"] = {"service_account_email": self._client.sae}

        task: dict = {"http_request": http_request}

        # Explicit Task Naming (Deduplication)
        if self.name:
            task["name"] = self.task_path

        # Dispatch Deadline (Timeout)
        if self.timeout:
            duration = duration_pb2.Duration()
            duration.FromSeconds(self.timeout)
            task["dispatch_deadline"] = duration

        # Scheduling
        if schedule_time:
            # If datetime is naive (no timezone), apply the client's default timezone
            if schedule_time.tzinfo is None:
                logger.debug(
                    f"Received naive datetime for schedule. Applying client timezone: {self._client.timezone}"
                )
                schedule_time = schedule_time.replace(tzinfo=self._client.timezone)

            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(schedule_time)
            task["schedule_time"] = timestamp

        return {
            "parent": self.queue_path,
            "task": task,
        }

    def _handle_error(self, e: Exception):
        logger.error(
            f"Failed to push task '{self._func.__name__}'. Error: {e}", exc_info=True
        )
        raise exceptions.TaskPushError(str(e))

    @property
    def data(self) -> dict[str, Any]:
        """Returns the payload data structure to be sent to the worker."""
        return {
            "func": self._func.__name__,
            "path": self.path,
            "args": self._func_args,
            "kwargs": self._func_kwargs,
        }

    @property
    def headers(self) -> dict[str, str]:
        """Constructs the headers dictionary, including custom and secret headers."""
        headers: dict = {}
        for name, value in self._headers.items():
            # Standardize header keys
            headers[name.replace("_", "-")] = str(value)

        if self._client.secret:
            headers[self._client.secret_header_name] = self._client.secret

        headers["Content-Type"] = "application/json"
        return headers

    @headers.setter
    def headers(self, headers: dict[str, str]):
        self._headers.update(headers)

    @property
    def is_coroutine(self) -> bool:
        return self._is_coroutine

    @property
    def path(self) -> str:
        """Returns the fully qualified dot-path of the function."""
        return self._func_path

    @property
    def task_path(self) -> str | None:
        """Returns the full GCP Resource path for the task name."""
        if self._client.eager or not self.name:
            return None

        return self._client.service_utils.task_path(
            self._client.project,
            self._client.location,
            self.queue,
            self.name,
        )

    @property
    def queue_path(self) -> str | None:
        """Returns the full GCP Resource path for the queue."""
        if self._client.eager:
            return None

        return self._client.service_utils.queue_path(
            self._client.project,
            self._client.location,
            self._client.force_to_queue or self.queue,
        )


class CloudTaskClient:
    """
    Main client for configuring and creating Cloud Tasks.
    Acts as a factory for decorators.
    """

    def __init__(
        self,
        queue: str,
        project: str,
        location: str,
        url: str,
        sae: str,
        timeout: int | None = None,
        secret: str = "",
        secret_header_name: str = "X-PYCT-SECRET",
        timezone: str | ZoneInfo | None = None,
        force_to_queue: str | None = None,
        eager: None | Literal["remote", "immediate"] = None,
    ):
        """
        Initializes the CloudTaskClient.

        Args:
            queue (str): Default Cloud Tasks queue name.
            project (str): GCP Project ID.
            location (str): GCP Region (e.g., 'europe-west1').
            url (str): Default target URL (worker endpoint).
            sae (str): Service Account Email for OIDC authentication.
            eager (bool, optional): If True, runs tasks locally instead of pushing to GCP. Defaults to False.
            timeout (int | None, optional): Default timeout for tasks in seconds. Defaults to None.
            secret (str, optional): A shared secret/token to send in headers for security. Defaults to "".
            secret_header_name (str, optional): The header name for the secret. Defaults to "X-PYCT-SECRET".
            timezone (str | ZoneInfo | None, optional): Default timezone for scheduling. Defaults to UTC.
            force_to_queue (str | NOne, optional): Usefull for dev and staging env, to for use the same queue
        """
        self.queue = queue
        self.location = location
        self.url = url
        self.project = project
        self.sae = sae
        self.eager = eager
        self.timeout = timeout
        self.secret = secret
        self.secret_header_name = secret_header_name
        self.force_to_queue = force_to_queue

        # Timezone Configuration
        if isinstance(timezone, str):
            self.timezone = ZoneInfo(timezone)
        else:
            self.timezone = timezone or ZoneInfo("UTC")

        self._sync_client = None
        self._async_client = None

        logger.debug(
            f"CloudTaskClient initialized. Eager: {eager}, Timezone: {self.timezone}"
        )

    def task(
        self,
        queue: str | None = None,
        name: str | None = None,
        url: str | None = None,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, Task[P, R]]]:
        """
        Decorator to convert a function into a Cloud Task.

        Args:
            queue (str | None, optional): Override default queue.
            name (str | None, optional): Explicit task name/ID.
            url (str | None, optional): Override default target URL.
            timeout (int | None, optional): Override default timeout.
            headers (dict | None, optional): Add specific headers.

        Returns:
            Callable: The decorated function which returns a Task object when called.
        """

        def decorator(func: Callable[P, R]) -> Callable[P, Task[P, R]]:
            @wraps(func)
            def inner(*args: P.args, **kwargs: P.kwargs) -> Task[P, R]:
                return Task(
                    func=func,
                    func_args=args,
                    func_kwargs=kwargs,
                    queue=queue or self.queue,
                    url=url or self.url,
                    timeout=timeout or self.timeout,
                    name=name,
                    headers=headers,
                    client=self,
                )

            return inner

        return decorator

    @property
    def sync_service(self):
        """Lazy load synchronous client (google.cloud.tasks_v2.CloudTasksClient)"""
        if self.eager:
            return None

        if not self._sync_client:
            self._sync_client = tasks_v2.CloudTasksClient()
        return self._sync_client

    @property
    def service(self):
        """Lazy load ASYNC client (google.cloud.tasks_v2.CloudTasksAsyncClient)"""
        if self.eager:
            return None
        if not self._async_client:
            self._async_client = tasks_v2.CloudTasksAsyncClient()
        return self._async_client

    @property
    def service_utils(self):
        """Helper to access paths (can use either client class, they share helpers)"""
        return tasks_v2.CloudTasksClient
