import json
from datetime import datetime
from functools import wraps
from logging import getLogger
from typing import Any, Callable, Generic, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from google.cloud import tasks_v2
from google.protobuf import duration_pb2, timestamp_pb2

from cloudtask import exceptions

# Type aliases for better readability in decorators
P = ParamSpec("P")
R = TypeVar("R")

logger = getLogger("cloudtask")


class Task(Generic[R]):
    """
    Represents a task wrapper that captures the function and its arguments,
    ready to be executed locally or pushed to Google Cloud Tasks.
    """

    def __init__(
        self,
        client: "CloudTaskClient",
        func: Callable[..., R],
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

    def __call__(self) -> R:
        """Allows the task instance to be called directly like the original function."""
        return self.execute()

    def execute(self) -> R:
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

    def delay(self) -> str | None:
        """
        Pushes the task to Google Cloud Tasks for immediate execution.

        Returns:
            str | None: The fully qualified task name if pushed, or None if executed eagerly.
        """
        return self._push()

    def schedule(self, at: datetime) -> str | None:
        """
        Schedules the task to be executed at a specific future time.

        Args:
            at (datetime): The specific time to run the task.

        Returns:
            str | None: The fully qualified task name if scheduled, or None if executed eagerly.
        """
        return self._push(schedule_time=at)

    # Alias for delay, common in other queuing systems
    push = delay

    def _push(self, schedule_time: datetime | None = None) -> str | None:
        """Internal method to handle the logic of sending the task to GCP."""

        # 1. Eager Mode Check
        if self._client.eager:
            logger.info(
                f"Eager mode enabled. Running task '{self._func.__name__}' locally."
            )
            self.execute()
            return "local-execution"

        task_payload = self._build_task_payload()

        # 2. Schedule Time Handling
        if schedule_time:
            # If datetime is naive (no timezone), apply the client's default timezone
            if schedule_time.tzinfo is None:
                logger.debug(
                    f"Received naive datetime for schedule. Applying client timezone: {self._client.timezone}"
                )
                schedule_time = schedule_time.replace(tzinfo=self._client.timezone)

            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(schedule_time)
            task_payload["schedule_time"] = timestamp

            logger.info(
                f"Scheduling task '{self._func.__name__}' for {schedule_time} on queue '{self.queue}'."
            )
        else:
            logger.info(
                f"Pushing task '{self._func.__name__}' to queue '{self.queue}'..."
            )

        # 3. API Call to Google
        try:
            response = self._client.service.create_task(
                request={
                    "parent": self.queue_path,
                    "task": task_payload,
                }
            )
            logger.info(f"Task successfully created: {response.name}")
            return response.name
        except Exception as e:
            logger.error(
                f"Failed to push task '{self._func.__name__}' to queue '{self.queue}'. Error: {str(e)}",
                exc_info=True,
            )
            raise exceptions.CloudTaskException(
                f"Failed to push task '{self._func.__name__}' to queue '{self.queue}'. Error: {str(e)}"
            )

    def _build_task_payload(self) -> dict:
        """Constructs the dictionary payload expected by Google Cloud Tasks API."""

        # Encoding payload to JSON bytes
        json_body = json.dumps(self.data).encode("utf-8")

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

        return task

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
    def path(self) -> str:
        """Returns the fully qualified dot-path of the function."""
        return self._func_path

    @property
    def task_path(self) -> str | None:
        """Returns the full GCP Resource path for the task name."""
        if self._client.eager or not self.name:
            return None

        return self._client.service.task_path(
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

        return self._client.service.queue_path(
            self._client.project,
            self._client.location,
            self.queue,
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
        eager: bool = False,
        timeout: int | None = None,
        secret: str = "",
        secret_header_name: str = "X-PYCT-SECRET",
        timezone: str | ZoneInfo | None = None,
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

        # Initialize Google Cloud Client (Lazy load if eager to avoid credentials error locally)
        self.service = tasks_v2.CloudTasksClient() if not eager else None

        # Timezone Configuration
        if isinstance(timezone, str):
            self.timezone = ZoneInfo(timezone)
        else:
            self.timezone = timezone or ZoneInfo("UTC")

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
    ) -> Callable[[Callable[P, R]], Callable[P, Task[R]]]:
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

        def decorator(func: Callable[P, R]) -> Callable[P, Task[R]]:
            @wraps(func)
            def inner(*args: P.args, **kwargs: P.kwargs) -> Task[R]:
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
